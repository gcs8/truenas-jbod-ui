from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from history_service.config import HistorySettings
from app.request_context import request_id_headers
from app.metrics import (
    observe_history_collection_run,
    observe_history_retention_run,
    set_history_collection_schedule_overrun,
    set_history_collector_running,
)
from history_service.domain import (
    MetricSample,
    SlotStateRecord,
    build_slot_events,
    isoformat_utc,
    normalize_text,
    utcnow,
)
from history_service.scheduled_backup import read_scheduled_backup_status
from history_service.store import HistoryStore, SlotStateUpdate

logger = logging.getLogger(__name__)
STORAGE_VIEW_SCOPE_PREFIX = "storage-view:"
HISTORY_METRICS_SERVICE_NAME = "enclosure-history"

FAST_METRIC_FIELDS = ("temperature_c",)
CACHED_SMART_REQUEST_TIMEOUT_SECONDS = 5
SLOW_METRIC_FIELDS = (
    "bytes_read",
    "bytes_written",
    "annualized_bytes_read",
    "annualized_bytes_written",
    "power_on_hours",
)
EXTENDED_STATE_FIELDS = (
    "persistent_id_label",
    "logical_unit_id",
    "sas_address",
    "topology_label",
    "multipath_device",
    "multipath_mode",
    "multipath_state",
    "multipath_lunid",
    "multipath_primary_path",
    "multipath_alternate_path",
    "multipath_active_paths",
    "multipath_passive_paths",
    "multipath_failed_paths",
    "multipath_other_paths",
    "multipath_active_controllers",
    "multipath_passive_controllers",
    "multipath_failed_controllers",
)
TOPOLOGY_FIELDS = ("pool_name", "vdev_name", "topology_label")
TOPOLOGY_CHANGE_CONFIRMATION_COUNT = 2
MASS_TOPOLOGY_DEGRADATION_MIN_SLOTS = 4
MASS_TOPOLOGY_DEGRADATION_RATIO = 0.25
SMART_FAILURE_STATUSES = {
    "BAD",
    "CRITICAL",
    "FAIL",
    "FAILED",
    "FAILING",
    "FAULT",
    "OFFLINE",
}


@dataclass(slots=True, frozen=True)
class ScopeSnapshot:
    system_id: str
    system_label: str | None
    enclosure_id: str | None
    enclosure_label: str | None
    snapshot: dict[str, Any]


class HistoryCollectionAlreadyRunning(RuntimeError):
    pass


class HistoryCollectionStopping(RuntimeError):
    pass


class HistoryCollector:
    def __init__(self, settings: HistorySettings, store: HistoryStore) -> None:
        self.settings = settings
        self.store = store
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.started_at = isoformat_utc()
        self.last_inventory_at: str | None = None
        self.last_fast_metrics_at: str | None = None
        self.last_slow_metrics_at: str | None = None
        self.last_success_at: str | None = None
        self.last_backup_at: str | None = None
        self.last_retention_at: str | None = None
        self.last_retention_backup_at: str | None = None
        self.last_retention_attempt_at: str | None = None
        self.last_retention_duration_seconds: float | None = None
        self.last_retention_rows_removed: int = 0
        self.last_retention_metric_samples_removed: int = 0
        self.last_retention_events_removed: int = 0
        self.last_retention_hourly_rollups_removed: int = 0
        self.last_retention_daily_rollups_removed: int = 0
        self.last_retention_has_more: bool = False
        self.last_retention_error: str | None = None
        self.last_error: str | None = None
        self.last_scope_count: int = 0
        self.current_collection_started_at: str | None = None
        self.current_collection_kind: str | None = None
        self.current_collection_activity: str | None = None
        self.current_collection_inventory_forced: bool | None = None
        self.current_collection_stage_timings: list[dict[str, Any]] = []
        self.last_collection_inventory_forced: bool | None = None
        self.last_collection_duration_seconds: float | None = None
        self.last_background_overrun_seconds: float = 0.0
        self.last_collection_stage_timings: list[dict[str, Any]] = []
        self.background_consecutive_failures: int = 0
        self.background_backoff_delay_seconds: int = 0
        self.background_backoff_until: datetime | None = None
        self.last_smart_failure_evidence_disks: int = 0
        self.last_max_temperature_celsius: float | None = None
        self.last_smart_evidence_at: str | None = None
        self.next_collection_at: datetime | None = None
        self._pending_topology_changes: dict[
            tuple[str, str, int],
            tuple[tuple[str | None, str | None, str | None], int],
        ] = {}
        self._run_lock = threading.Lock()
        set_history_collector_running(HISTORY_METRICS_SERVICE_NAME, False)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name="history-collector")
        set_history_collector_running(HISTORY_METRICS_SERVICE_NAME, True)

    async def stop(self) -> None:
        task = self._task
        self._stopping.set()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._wait_for_collection_quiescence)
        if task:
            if self._task is task:
                self._task = None
        set_history_collector_running(HISTORY_METRICS_SERVICE_NAME, False)

    async def run_once(
        self,
        *,
        force_fast: bool = False,
        force_slow: bool = False,
        include_due_intervals: bool = True,
        cached_root_only: bool = False,
        collection_kind: str = "manual",
    ) -> None:
        await asyncio.to_thread(
            self._run_once_blocking,
            force_fast,
            force_slow,
            include_due_intervals,
            cached_root_only,
            collection_kind,
        )

    def _run_once_blocking(
        self,
        force_fast: bool,
        force_slow: bool,
        include_due_intervals: bool,
        cached_root_only: bool,
        collection_kind: str,
    ) -> None:
        if not self._run_lock.acquire(blocking=False):
            raise HistoryCollectionAlreadyRunning("History collection already running.")
        collection_started_monotonic = time.perf_counter()
        self.current_collection_started_at = isoformat_utc()
        self.current_collection_kind = collection_kind
        self.current_collection_activity = "starting collection"
        self.current_collection_inventory_forced = None
        self.current_collection_stage_timings = []
        try:
            asyncio.run(
                self._run_once_unlocked(
                    force_fast=force_fast,
                    force_slow=force_slow,
                    include_due_intervals=include_due_intervals,
                    cached_root_only=cached_root_only,
                )
            )
        finally:
            self.last_collection_duration_seconds = round(time.perf_counter() - collection_started_monotonic, 3)
            self.last_collection_inventory_forced = self.current_collection_inventory_forced
            self.last_collection_stage_timings = list(self.current_collection_stage_timings)
            self.current_collection_started_at = None
            self.current_collection_kind = None
            self.current_collection_activity = None
            self.current_collection_inventory_forced = None
            self.current_collection_stage_timings = []
            self._run_lock.release()

    async def _run_once_unlocked(
        self,
        *,
        force_fast: bool = False,
        force_slow: bool = False,
        include_due_intervals: bool = True,
        cached_root_only: bool = False,
    ) -> None:
        self._raise_if_stopping()
        run_started = utcnow()
        observed_at = isoformat_utc(run_started)
        collect_fast = force_fast or (
            include_due_intervals
            and self._interval_due(
                self.last_fast_metrics_at,
                self.settings.fast_interval_seconds,
                run_started,
            )
        )
        collect_slow = force_slow or (
            include_due_intervals
            and self._interval_due(
                self.last_slow_metrics_at,
                self.settings.slow_interval_seconds,
                run_started,
            )
        )
        smart_evidence_expected: set[str] = set()
        smart_evidence_observed: set[str] = set()
        smart_failure_evidence: set[str] = set()
        smart_temperatures: dict[str, float] = {}
        force_inventory = self._should_force_inventory_for_collection(
            collect_fast=collect_fast,
            collect_slow=collect_slow,
        )
        self.current_collection_inventory_forced = force_inventory
        inventory_mode = "forced" if force_inventory else "cached"
        self._set_collection_activity(f"enumerating systems and enclosures ({inventory_mode} inventory)")
        enumerate_started = time.perf_counter()
        enumerate_kwargs: dict[str, bool] = {"force_inventory": force_inventory}
        if cached_root_only and not force_inventory:
            enumerate_kwargs["cached_root_only"] = True
        scopes = await self._enumerate_scopes(**enumerate_kwargs)
        self._raise_if_stopping()
        self._record_collection_stage(
            "enumerate.scopes",
            time.perf_counter() - enumerate_started,
            inventory_forced=force_inventory,
            scope_count=len(scopes),
        )
        self.last_scope_count = len(scopes)
        self.last_inventory_at = observed_at

        for scope_index, scope in enumerate(scopes, start=1):
            self._raise_if_stopping()
            scope_label = self._scope_activity_label(scope)
            self._set_collection_activity(f"recording {scope_label} ({scope_index}/{len(scopes)})")
            if not self._should_record_scope_snapshot(scope.snapshot):
                logger.warning(
                    "Skipping history capture for %s%s because the inventory snapshot is degraded or untrusted.",
                    scope.system_id,
                    f" enclosure {scope.enclosure_id}" if scope.enclosure_id else "",
                )
                self._record_collection_stage(
                    "scope.skipped",
                    0.0,
                    system_id=scope.system_id,
                    system_label=scope.system_label,
                    enclosure_id=scope.enclosure_id,
                    enclosure_label=scope.enclosure_label,
                    scope_index=scope_index,
                )
                continue
            slot_records = [
                SlotStateRecord.from_snapshot_slot(scope.snapshot, slot_payload)
                for slot_payload in scope.snapshot.get("slots", [])
            ]
            slot_state_started = time.perf_counter()
            self._raise_if_stopping()
            self._record_slot_changes(slot_records, observed_at)
            self._record_collection_stage(
                "db.slot_state",
                time.perf_counter() - slot_state_started,
                system_id=scope.system_id,
                system_label=scope.system_label,
                enclosure_id=scope.enclosure_id,
                enclosure_label=scope.enclosure_label,
                scope_index=scope_index,
                slot_count=len(slot_records),
            )

            if not collect_fast and not collect_slow:
                continue

            present_slots = [record.slot for record in slot_records if record.present]
            if not present_slots:
                continue
            records_by_slot = {record.slot: record for record in slot_records if record.present}
            for record in records_by_slot.values():
                smart_evidence_expected.add(self._smart_evidence_disk_key(record))
            self._set_collection_activity(f"collecting SMART metrics for {scope_label} ({scope_index}/{len(scopes)})")
            smart_started = time.perf_counter()
            try:
                summaries = await self._fetch_smart_summaries(scope, present_slots, force_fresh=collect_slow)
            except Exception as exc:  # noqa: BLE001 - one slow scope should not fail the whole fleet pass.
                logger.warning(
                    "Skipping history SMART metrics for %s: %s",
                    scope_label,
                    exc,
                )
                self._record_collection_stage(
                    "smart.failed",
                    time.perf_counter() - smart_started,
                    system_id=scope.system_id,
                    system_label=scope.system_label,
                    enclosure_id=scope.enclosure_id,
                    enclosure_label=scope.enclosure_label,
                    scope_index=scope_index,
                    slot_count=len(present_slots),
                    force_fresh=collect_slow,
                    error=str(exc),
                )
                continue
            self._raise_if_stopping()
            self._record_collection_stage(
                "smart.fresh" if collect_slow else "smart.cached",
                time.perf_counter() - smart_started,
                system_id=scope.system_id,
                system_label=scope.system_label,
                enclosure_id=scope.enclosure_id,
                enclosure_label=scope.enclosure_label,
                scope_index=scope_index,
                slot_count=len(present_slots),
                summary_count=len(summaries),
                force_fresh=collect_slow,
            )
            metric_samples: list[MetricSample] = []
            for record in slot_records:
                summary = summaries.get(record.slot)
                if not isinstance(summary, dict):
                    continue
                evidence_key = self._smart_evidence_disk_key(record)
                if self._smart_summary_has_alert_evidence(summary):
                    smart_evidence_observed.add(evidence_key)
                    if self._smart_summary_indicates_failure(summary):
                        smart_failure_evidence.add(evidence_key)
                    temperature = self._smart_summary_temperature(summary)
                    if temperature is not None:
                        previous_temperature = smart_temperatures.get(evidence_key)
                        if previous_temperature is None or temperature > previous_temperature:
                            smart_temperatures[evidence_key] = temperature
                if collect_fast:
                    metric_samples.extend(
                        self._build_metric_samples(record, summary, observed_at, FAST_METRIC_FIELDS)
                    )
                if collect_slow:
                    metric_samples.extend(
                        self._build_metric_samples(record, summary, observed_at, SLOW_METRIC_FIELDS)
                    )
            if metric_samples:
                metrics_started = time.perf_counter()
                self._raise_if_stopping()
                self.store.insert_metric_samples(metric_samples)
                self._record_collection_stage(
                    "db.metrics",
                    time.perf_counter() - metrics_started,
                    system_id=scope.system_id,
                    system_label=scope.system_label,
                    enclosure_id=scope.enclosure_id,
                    enclosure_label=scope.enclosure_label,
                    scope_index=scope_index,
                    sample_count=len(metric_samples),
                )

        if (collect_fast or collect_slow) and smart_evidence_expected.issubset(smart_evidence_observed):
            self.last_smart_failure_evidence_disks = len(smart_failure_evidence)
            self.last_max_temperature_celsius = (
                max(smart_temperatures.values()) if smart_temperatures else None
            )
            self.last_smart_evidence_at = observed_at

        backup_succeeded = False
        retention_backup_at: datetime | None = None
        if collect_fast:
            self.last_fast_metrics_at = observed_at
        if collect_slow:
            self.last_slow_metrics_at = observed_at
            self._raise_if_stopping()
            if self.settings.segment_catalog_path is not None:
                retention_backup_at = self._segmented_backup_at_for_retention(run_started)
                backup_succeeded = retention_backup_at is not None
                if retention_backup_at is not None:
                    self.last_backup_at = isoformat_utc(retention_backup_at)
                self._record_collection_stage(
                    "db.backup.skipped",
                    0.0,
                    reason="segmented_history_uses_scheduled_full_backup",
                    scheduled_full_backup_ready=backup_succeeded,
                )
            else:
                try:
                    latest_backup_at = self._latest_backup_at()
                    if not self._backup_due(run_started, latest_backup_at=latest_backup_at):
                        self._record_collection_stage(
                            "db.backup.skipped",
                            0.0,
                            reason="recent_backup",
                            interval_seconds=max(0, int(self.settings.backup_interval_seconds or 0)),
                            latest_backup_at=isoformat_utc(latest_backup_at) if latest_backup_at else None,
                        )
                        if latest_backup_at:
                            self.last_backup_at = isoformat_utc(latest_backup_at)
                    else:
                        self._set_collection_activity("creating history database backup")
                        backup_started = time.perf_counter()
                        self._raise_if_stopping()
                        backup_path = self.store.create_backup(
                            self.settings.backup_dir,
                            snapshot_label=observed_at,
                            retention_count=self.settings.backup_retention_count,
                            long_term_backup_dir=self.settings.long_term_backup_dir,
                            weekly_retention_count=self.settings.weekly_backup_retention_count,
                            monthly_retention_count=self.settings.monthly_backup_retention_count,
                        )
                        self._record_collection_stage(
                            "db.backup",
                            time.perf_counter() - backup_started,
                            backup_created=bool(backup_path),
                        )
                        if backup_path:
                            self.last_backup_at = observed_at
                            retention_backup_at = run_started
                            backup_succeeded = True
                except Exception as exc:  # noqa: BLE001 - collection continues after backup failure.
                    logger.warning("History backup snapshot failed: %s", exc)
        self._raise_if_stopping()
        self._run_retention_if_due(
            run_started,
            backup_succeeded=backup_succeeded,
            backup_at=retention_backup_at,
        )
        self.last_success_at = observed_at
        self.last_error = None
        self._set_collection_activity("collection completed")
        self._clear_background_failure_backoff()

    def status(self) -> dict[str, Any]:
        collection_started_at = self.current_collection_started_at
        return {
            "collector_running": bool(self._task and not self._task.done()),
            "collection_running": self.collection_running,
            "collection_started_at": collection_started_at,
            "collection_kind": self.current_collection_kind,
            "collection_activity": self.current_collection_activity,
            "collection_elapsed_seconds": self._elapsed_seconds_since(collection_started_at),
            "collection_inventory_forced": self.current_collection_inventory_forced,
            "collection_stage_timings": list(self.current_collection_stage_timings)
            if collection_started_at
            else list(self.last_collection_stage_timings),
            "last_collection_inventory_forced": self.last_collection_inventory_forced,
            "last_collection_duration_seconds": self.last_collection_duration_seconds,
            "last_background_overrun_seconds": self.last_background_overrun_seconds,
            "poll_interval_seconds": max(1, int(self.settings.poll_interval_seconds)),
            "background_consecutive_failures": self.background_consecutive_failures,
            "background_backoff_delay_seconds": self.background_backoff_delay_seconds,
            "failure_backoff_max_seconds": max(1, int(self.settings.failure_backoff_max_seconds)),
            "background_backoff_until": isoformat_utc(self.background_backoff_until)
            if self.background_backoff_until
            else None,
            "background_backoff_seconds_remaining": self.background_backoff_seconds_remaining,
            "next_collection_at": isoformat_utc(self.next_collection_at) if self.next_collection_at else None,
            "started_at": self.started_at,
            "last_inventory_at": self.last_inventory_at,
            "last_fast_metrics_at": self.last_fast_metrics_at,
            "last_slow_metrics_at": self.last_slow_metrics_at,
            "last_smart_failure_evidence_disks": self.last_smart_failure_evidence_disks,
            "last_max_temperature_celsius": self.last_max_temperature_celsius,
            "last_smart_evidence_at": self.last_smart_evidence_at,
            "last_success_at": self.last_success_at,
            "last_backup_at": self.last_backup_at,
            "last_retention_at": self.last_retention_at,
            "last_retention_backup_at": self.last_retention_backup_at,
            "last_retention_attempt_at": self.last_retention_attempt_at,
            "last_retention_duration_seconds": self.last_retention_duration_seconds,
            "last_retention_rows_removed": self.last_retention_rows_removed,
            "last_retention_metric_samples_removed": self.last_retention_metric_samples_removed,
            "last_retention_events_removed": self.last_retention_events_removed,
            "last_retention_hourly_rollups_removed": self.last_retention_hourly_rollups_removed,
            "last_retention_daily_rollups_removed": self.last_retention_daily_rollups_removed,
            "last_retention_has_more": self.last_retention_has_more,
            "last_retention_error": self.last_retention_error,
            "last_error": self.last_error,
            "last_scope_count": self.last_scope_count,
            "source_base_url": self.settings.source_base_url,
            "sqlite_path": self.settings.sqlite_path,
        }

    @property
    def collection_running(self) -> bool:
        return self._run_lock.locked()

    @property
    def background_backoff_seconds_remaining(self) -> int:
        if not self.background_backoff_until:
            return 0
        remaining = (self.background_backoff_until - utcnow()).total_seconds()
        return max(0, int(remaining + 0.999))

    async def _run_loop(self) -> None:
        if self.settings.startup_grace_seconds > 0:
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.settings.startup_grace_seconds,
                )
            except asyncio.TimeoutError:
                pass

        while not self._stopping.is_set():
            if self.collection_running:
                logger.info("Skipping scheduled history collection because another collection pass is already running.")
                target_interval = (
                    min(self.settings.poll_interval_seconds, 30)
                    if self.last_success_at is None
                    else self.settings.poll_interval_seconds
                )
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=max(1.0, target_interval))
                except asyncio.TimeoutError:
                    pass
                continue
            if self.background_backoff_seconds_remaining > 0:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=min(max(1.0, self.background_backoff_seconds_remaining), 30.0),
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            started_monotonic = time.perf_counter()
            try:
                force_startup_collection = self.last_success_at is None
                self.next_collection_at = None
                run_kwargs = (
                    {
                        "force_fast": True,
                        "force_slow": False,
                        "include_due_intervals": False,
                        "cached_root_only": True,
                    }
                    if force_startup_collection
                    else {
                        "force_fast": False,
                        "force_slow": False,
                        "include_due_intervals": True,
                        "cached_root_only": False,
                    }
                )
                await self.run_once(
                    **run_kwargs,
                    collection_kind="background",
                )
                observe_history_collection_run(
                    service_name=HISTORY_METRICS_SERVICE_NAME,
                    result="success",
                    duration_seconds=time.perf_counter() - started_monotonic,
                    status=self.status(),
                    counts=self.store.estimated_counts(),
                )
            except HistoryCollectionAlreadyRunning:
                logger.info("Skipping scheduled history collection because another collection pass is already running.")
            except HistoryCollectionStopping:
                logger.info("Stopping the scheduled history collection at a safe stage boundary.")
                break
            except Exception as exc:  # noqa: BLE001 - keep the collector alive across transient appliance errors.
                logger.exception("History collection pass failed")
                self.last_error = str(exc)
                self._record_background_failure(utcnow())
                observe_history_collection_run(
                    service_name=HISTORY_METRICS_SERVICE_NAME,
                    result="error",
                    duration_seconds=time.perf_counter() - started_monotonic,
                    status=self.status(),
                    counts=None,
                )

            if self.background_backoff_seconds_remaining > 0:
                sleep_for = min(max(1.0, self.background_backoff_seconds_remaining), 30.0)
            else:
                target_interval = (
                    min(self.settings.poll_interval_seconds, 30)
                    if self.last_success_at is None
                    else self.settings.poll_interval_seconds
                )
                sleep_for = self._schedule_next_background_collection(
                    started_monotonic=started_monotonic,
                    target_interval=max(1.0, float(target_interval)),
                )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                continue

    def _record_background_failure(self, now: datetime) -> None:
        self.background_consecutive_failures += 1
        delay_seconds = self._background_failure_delay_seconds()
        self.background_backoff_delay_seconds = delay_seconds
        self.background_backoff_until = now + timedelta(seconds=delay_seconds)
        self.next_collection_at = self.background_backoff_until
        logger.warning(
            "History background collection failure count=%s; backing off for %s seconds until %s.",
            self.background_consecutive_failures,
            delay_seconds,
            isoformat_utc(self.background_backoff_until),
        )

    def _clear_background_failure_backoff(self) -> None:
        if self.background_consecutive_failures or self.background_backoff_until:
            logger.info("History background collection recovered; clearing failure backoff.")
        self.background_consecutive_failures = 0
        self.background_backoff_delay_seconds = 0
        self.background_backoff_until = None

    def _background_failure_delay_seconds(self) -> int:
        initial = max(1, int(self.settings.failure_backoff_initial_seconds or 1))
        maximum = max(initial, int(self.settings.failure_backoff_max_seconds or initial))
        exponent = min(max(0, self.background_consecutive_failures - 1), 20)
        return min(maximum, initial * (2**exponent))

    def _schedule_next_collection_after(self, seconds: float) -> None:
        self.next_collection_at = utcnow() + timedelta(seconds=max(1.0, seconds))

    def _raise_if_stopping(self) -> None:
        if self._stopping.is_set():
            raise HistoryCollectionStopping("History collection is stopping.")

    def _wait_for_collection_quiescence(self) -> None:
        self._run_lock.acquire()
        self._run_lock.release()

    def _schedule_next_background_collection(
        self,
        *,
        started_monotonic: float,
        target_interval: float,
    ) -> float:
        elapsed = max(0.0, time.perf_counter() - started_monotonic)
        overrun = max(0.0, elapsed - target_interval)
        self.last_background_overrun_seconds = round(overrun, 3)
        set_history_collection_schedule_overrun(
            HISTORY_METRICS_SERVICE_NAME,
            self.last_background_overrun_seconds,
        )
        if overrun <= 0:
            sleep_for = max(0.0, target_interval - elapsed)
        else:
            interval_remainder = elapsed % target_interval
            sleep_for = (
                target_interval
                if interval_remainder < 1e-9
                else target_interval - interval_remainder
            )
        self.next_collection_at = utcnow() + timedelta(seconds=sleep_for)
        if overrun > 0:
            logger.warning(
                "History background collection overran its %.1f-second interval by %.1f seconds.",
                target_interval,
                overrun,
            )
        return sleep_for

    def _set_collection_activity(self, message: str) -> None:
        self.current_collection_activity = message

    def _record_collection_stage(self, stage: str, duration_seconds: float, **metadata: Any) -> None:
        entry: dict[str, Any] = {
            "stage": stage,
            "duration_ms": round(max(0.0, duration_seconds) * 1000, 1),
        }
        for key, value in metadata.items():
            if value is None or value == "":
                continue
            entry[key] = value
        self.current_collection_stage_timings.append(entry)
        if len(self.current_collection_stage_timings) > 120:
            del self.current_collection_stage_timings[: len(self.current_collection_stage_timings) - 120]

    def _should_force_inventory_for_collection(
        self,
        *,
        collect_fast: bool,
        collect_slow: bool,
    ) -> bool:
        if collect_slow:
            return True
        if collect_fast and self.settings.force_inventory_on_fast_collection:
            return True
        return False

    def _segmented_backup_at_for_retention(self, now: datetime) -> datetime | None:
        status_path = self.settings.scheduled_backup_status_file
        if not status_path:
            return None
        status = read_scheduled_backup_status(status_path)
        if (
            status is None
            or status.get("last_error_code") is not None
            or "history_db" not in status.get("included_groups", [])
            or "history_db" in status.get("last_absent_groups", [])
        ):
            return None
        raw_success_at = status.get("last_success_at")
        if not isinstance(raw_success_at, str):
            return None
        try:
            success_at = datetime.fromisoformat(raw_success_at)
        except ValueError:
            return None
        if success_at.tzinfo is None:
            return None
        success_at = success_at.astimezone(timezone.utc)
        normalized_now = now.astimezone(timezone.utc)
        age = normalized_now - success_at
        if age < timedelta(0) or age > timedelta(
            seconds=self.settings.segmented_backup_max_age_seconds
        ):
            return None
        return success_at

    def _run_retention_if_due(
        self,
        now: datetime,
        *,
        backup_succeeded: bool,
        backup_at: datetime | None = None,
    ) -> None:
        if not backup_succeeded or not self._retention_due(now):
            return
        segmented = self.settings.segment_catalog_path is not None
        if segmented and backup_at is None:
            return
        started = time.perf_counter()
        attempted_at = isoformat_utc(now)

        def maintain_retention() -> dict[str, Any]:
            self.last_retention_attempt_at = attempted_at
            if backup_at is not None:
                self.last_retention_backup_at = isoformat_utc(backup_at)
            self._set_collection_activity("pruning and rolling up history")
            return self.store.maintain_retention(
                now=now,
                raw_metric_retention_days=self.settings.raw_metric_retention_days,
                event_retention_days=self.settings.event_retention_days,
                hourly_rollup_retention_days=self.settings.hourly_rollup_retention_days,
                daily_rollup_retention_days=self.settings.daily_rollup_retention_days,
                batch_size=self.settings.retention_batch_size,
                max_batches=self.settings.retention_max_batches_per_run,
                should_continue=lambda: not self._stopping.is_set(),
                migration_lock_held=segmented,
            )

        try:
            if segmented:
                assert backup_at is not None
                result = self.store.run_segmented_retention(
                    backup_at,
                    maintain_retention,
                )
                if result is None:
                    return
            else:
                result = maintain_retention()
        except Exception as exc:  # noqa: BLE001 - retention failure must not stop collection.
            duration = time.perf_counter() - started
            self.last_retention_duration_seconds = round(duration, 3)
            self.last_retention_error = type(exc).__name__
            partial_result = getattr(exc, "retention_summary", None)
            if not isinstance(partial_result, dict):
                partial_result = {}
            removed_rows = self._apply_retention_result(
                {**partial_result, "has_more": True}
            )
            logger.warning(
                "History retention pass failed with %s; collection will continue.",
                type(exc).__name__,
            )
            observe_history_retention_run(
                service_name=HISTORY_METRICS_SERVICE_NAME,
                result="error",
                duration_seconds=duration,
                removed_rows=removed_rows,
                completed_at=attempted_at,
            )
            self._record_collection_stage(
                "db.retention.failed",
                duration,
                error_type=type(exc).__name__,
            )
            return

        duration = time.perf_counter() - started
        self.last_retention_at = attempted_at
        self.last_retention_duration_seconds = round(duration, 3)
        self.last_retention_error = None
        removed_rows = self._apply_retention_result(result)
        observe_history_retention_run(
            service_name=HISTORY_METRICS_SERVICE_NAME,
            result="success",
            duration_seconds=duration,
            removed_rows=removed_rows,
            completed_at=attempted_at,
        )
        self._record_collection_stage(
            "db.retention",
            duration,
            rows_removed=self.last_retention_rows_removed,
            batches_completed=int(result.get("batches_completed") or 0),
            has_more=self.last_retention_has_more,
            interrupted=bool(result.get("interrupted")),
        )
        if self.last_retention_rows_removed:
            logger.info(
                "History retention removed %s rows in %.3f seconds; has_more=%s.",
                self.last_retention_rows_removed,
                duration,
                self.last_retention_has_more,
            )

    def _apply_retention_result(self, result: dict[str, Any]) -> dict[str, int]:
        self.last_retention_rows_removed = int(result.get("total_rows_removed") or 0)
        self.last_retention_metric_samples_removed = int(result.get("metric_samples_removed") or 0)
        self.last_retention_events_removed = int(result.get("events_removed") or 0)
        self.last_retention_hourly_rollups_removed = int(result.get("hourly_rollups_removed") or 0)
        self.last_retention_daily_rollups_removed = int(result.get("daily_rollups_removed") or 0)
        self.last_retention_has_more = bool(result.get("has_more"))
        return {
            "metric_samples": self.last_retention_metric_samples_removed,
            "events": self.last_retention_events_removed,
            "hourly_rollups": self.last_retention_hourly_rollups_removed,
            "daily_rollups": self.last_retention_daily_rollups_removed,
        }

    def _retention_due(self, now: datetime) -> bool:
        if self.last_retention_has_more:
            return True
        if not self.last_retention_attempt_at:
            return True
        try:
            attempted_at = datetime.fromisoformat(self.last_retention_attempt_at)
        except ValueError:
            return True
        if attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=timezone.utc)
        interval = max(1, int(self.settings.retention_interval_seconds))
        return now.astimezone(timezone.utc) - attempted_at.astimezone(timezone.utc) >= timedelta(
            seconds=interval
        )

    def _latest_backup_at(self) -> datetime | None:
        if self.last_backup_at:
            try:
                latest = datetime.fromisoformat(self.last_backup_at)
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                return latest.astimezone(timezone.utc)
            except ValueError:
                pass
        return self.store.latest_backup_snapshot_at(self.settings.backup_dir)

    def _backup_due(self, now: datetime, *, latest_backup_at: datetime | None = None) -> bool:
        interval_seconds = max(0, int(self.settings.backup_interval_seconds or 0))
        if interval_seconds <= 0:
            return True
        latest = latest_backup_at or self._latest_backup_at()
        if latest is None:
            return True
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc) - latest.astimezone(timezone.utc) >= timedelta(seconds=interval_seconds)

    @staticmethod
    def _elapsed_seconds_since(value: str | None) -> int | None:
        if not value:
            return None
        try:
            started_at = datetime.fromisoformat(value)
        except ValueError:
            return None
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        return max(0, int((utcnow() - started_at.astimezone(timezone.utc)).total_seconds()))

    @staticmethod
    def _scope_activity_label(scope: ScopeSnapshot) -> str:
        system_label = scope.system_label or scope.system_id or "unknown system"
        enclosure_label = scope.enclosure_label or scope.enclosure_id or "default view"
        return f"{system_label} / {enclosure_label}"

    def _record_slot_changes(self, slot_records: list[SlotStateRecord], observed_at: str) -> None:
        suppressed_topology_degradations = 0
        pending_topology_changes = 0
        # One read per scope and one write transaction per pass instead of three
        # connection open/PRAGMA/commit cycles per slot.
        previous_by_scope: dict[tuple[str, str], dict[int, SlotStateRecord]] = {}
        updates: list[SlotStateUpdate] = []
        for record in slot_records:
            scope_key = (record.system_id, record.enclosure_key)
            if scope_key not in previous_by_scope:
                previous_by_scope[scope_key] = self.store.get_slot_states(record.system_id, record.enclosure_id)
            previous = previous_by_scope[scope_key].get(record.slot)
            if self._should_backfill_extended_state(previous, record):
                updates.append(SlotStateUpdate(record=record, observed_at=observed_at))
                continue
            record, topology_degraded, topology_pending = self._prepare_slot_record_for_history(
                previous,
                record,
            )
            if topology_degraded:
                suppressed_topology_degradations += 1
            if topology_pending:
                pending_topology_changes += 1
            events = build_slot_events(previous, record, observed_at)
            updates.append(SlotStateUpdate(record=record, observed_at=observed_at, events=list(events)))
        self.store.record_slot_updates(updates)
        if suppressed_topology_degradations:
            if self._is_mass_topology_degradation(suppressed_topology_degradations, len(slot_records)):
                logger.warning(
                    "Suppressed mass topology-detail degradation for %s/%s slots; keeping last trusted topology until the source stabilizes.",
                    suppressed_topology_degradations,
                    len(slot_records),
                )
            else:
                logger.info(
                    "Suppressed topology-detail degradation for %s/%s slots; keeping last trusted topology.",
                    suppressed_topology_degradations,
                    len(slot_records),
                )
        if pending_topology_changes:
            logger.info(
                "Holding %s topology changes pending a second consecutive observation before writing durable history events.",
                pending_topology_changes,
            )

    def _prepare_slot_record_for_history(
        self,
        previous: SlotStateRecord | None,
        current: SlotStateRecord,
    ) -> tuple[SlotStateRecord, bool, bool]:
        if previous is None:
            return current, False, False
        key = self._slot_state_key(current)
        if self._is_topology_degradation(previous, current):
            self._pending_topology_changes.pop(key, None)
            return self._with_previous_topology(previous, current), True, False
        if not self._topology_changed(previous, current):
            self._pending_topology_changes.pop(key, None)
            return current, False, False
        if not self._has_stable_disk_identity(previous, current):
            self._pending_topology_changes.pop(key, None)
            return current, False, False

        signature = self._topology_signature(current)
        pending_signature, pending_count = self._pending_topology_changes.get(key, ((None, None, None), 0))
        pending_count = pending_count + 1 if pending_signature == signature else 1
        if pending_count < TOPOLOGY_CHANGE_CONFIRMATION_COUNT:
            self._pending_topology_changes[key] = (signature, pending_count)
            return self._with_previous_topology(previous, current), False, True
        self._pending_topology_changes.pop(key, None)
        return current, False, False

    @staticmethod
    def _slot_state_key(record: SlotStateRecord) -> tuple[str, str, int]:
        return (record.system_id, record.enclosure_id or "", record.slot)

    @staticmethod
    def _topology_signature(record: SlotStateRecord) -> tuple[str | None, str | None, str | None]:
        return (record.pool_name, record.vdev_name, record.topology_label)

    @staticmethod
    def _topology_changed(previous: SlotStateRecord, current: SlotStateRecord) -> bool:
        return any(getattr(previous, field_name) != getattr(current, field_name) for field_name in TOPOLOGY_FIELDS)

    @staticmethod
    def _topology_detail_score(record: SlotStateRecord) -> int:
        score = 0
        if record.pool_name:
            score += 1
        if record.vdev_name:
            score += 4
        if record.topology_label:
            score += len([part for part in record.topology_label.split(">") if part.strip()])
        return score

    @staticmethod
    def _has_stable_disk_identity(previous: SlotStateRecord, current: SlotStateRecord) -> bool:
        if previous.present != current.present:
            return False
        for field_name in ("serial", "model", "gptid", "logical_unit_id", "disk_identity_key"):
            if getattr(previous, field_name) != getattr(current, field_name):
                return False
        return True

    @classmethod
    def _is_topology_degradation(cls, previous: SlotStateRecord, current: SlotStateRecord) -> bool:
        if not cls._topology_changed(previous, current):
            return False
        if not cls._has_stable_disk_identity(previous, current):
            return False
        if previous.pool_name != current.pool_name:
            return False
        return cls._topology_detail_score(current) < cls._topology_detail_score(previous)

    @staticmethod
    def _with_previous_topology(previous: SlotStateRecord, current: SlotStateRecord) -> SlotStateRecord:
        return replace(
            current,
            pool_name=previous.pool_name,
            vdev_name=previous.vdev_name,
            topology_label=previous.topology_label,
        )

    @staticmethod
    def _is_mass_topology_degradation(degraded_slots: int, total_slots: int) -> bool:
        if total_slots <= 0:
            return False
        return (
            degraded_slots >= MASS_TOPOLOGY_DEGRADATION_MIN_SLOTS
            and degraded_slots / total_slots >= MASS_TOPOLOGY_DEGRADATION_RATIO
        )

    @staticmethod
    def _should_backfill_extended_state(
        previous: SlotStateRecord | None,
        current: SlotStateRecord,
    ) -> bool:
        if previous is None:
            return False
        if any(getattr(previous, field_name) is not None for field_name in EXTENDED_STATE_FIELDS):
            return False
        if not any(getattr(current, field_name) is not None for field_name in EXTENDED_STATE_FIELDS):
            return False
        stable_fields = (
            "present",
            "state",
            "identify_active",
            "device_name",
            "serial",
            "model",
            "gptid",
            "pool_name",
            "vdev_name",
            "health",
        )
        return all(getattr(previous, field_name) == getattr(current, field_name) for field_name in stable_fields)

    @staticmethod
    def _should_record_scope_snapshot(snapshot: dict[str, Any]) -> bool:
        sources = snapshot.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        api_source = sources.get("api")
        if isinstance(api_source, dict) and api_source.get("enabled") and not api_source.get("ok"):
            return False
        if normalize_text(snapshot.get("selected_system_platform")) == "quantastor":
            platform_context = snapshot.get("platform_context")
            if isinstance(platform_context, dict) and platform_context.get("topology_complete") is False:
                return False
        return True

    @staticmethod
    def _smart_evidence_disk_key(record: SlotStateRecord) -> str:
        if record.disk_identity_key:
            return record.disk_identity_key
        serial = (normalize_text(record.serial) or "").casefold()
        logical_unit_id = (normalize_text(record.logical_unit_id) or "").casefold()
        sas_address = (normalize_text(record.sas_address) or "").casefold()
        if serial and logical_unit_id:
            return "\x1f".join(("logical-unit", serial, logical_unit_id))
        if serial and sas_address:
            return "\x1f".join(("sas", serial, sas_address))
        return "\x1f".join(("slot", record.system_id, record.enclosure_key, str(record.slot)))

    @staticmethod
    def _smart_summary_has_alert_evidence(summary: dict[str, Any]) -> bool:
        return any(
            summary.get(field_name) is not None
            for field_name in ("smart_health_status", "predictive_errors", "temperature_c")
        )

    @staticmethod
    def _smart_summary_indicates_failure(summary: dict[str, Any]) -> bool:
        health_status = (normalize_text(summary.get("smart_health_status")) or "").upper()
        predictive_errors = summary.get("predictive_errors")
        has_predictive_errors = (
            isinstance(predictive_errors, int | float)
            and not isinstance(predictive_errors, bool)
            and predictive_errors > 0
        )
        return health_status in SMART_FAILURE_STATUSES or has_predictive_errors

    @staticmethod
    def _smart_summary_temperature(summary: dict[str, Any]) -> float | None:
        value = summary.get("temperature_c")
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        temperature = float(value)
        return temperature if math.isfinite(temperature) else None

    def _build_metric_samples(
        self,
        slot_record: SlotStateRecord,
        summary: dict[str, Any],
        observed_at: str,
        field_names: tuple[str, ...],
    ) -> list[MetricSample]:
        samples: list[MetricSample] = []
        for field_name in field_names:
            value = summary.get(field_name)
            if value is None:
                continue
            if isinstance(value, bool):
                value_integer = int(value)
                value_real = None
            elif isinstance(value, int):
                value_integer = value
                value_real = None
            elif isinstance(value, float):
                value_integer = None
                value_real = value
            else:
                continue
            samples.append(
                MetricSample(
                    observed_at=observed_at,
                    system_id=slot_record.system_id,
                    system_label=slot_record.system_label,
                    enclosure_key=slot_record.enclosure_key,
                    enclosure_id=slot_record.enclosure_id,
                    enclosure_label=slot_record.enclosure_label,
                    slot=slot_record.slot,
                    slot_label=slot_record.slot_label,
                    metric_name=field_name,
                    value_integer=value_integer,
                    value_real=value_real,
                    device_name=slot_record.device_name,
                    serial=slot_record.serial,
                    model=slot_record.model,
                    state=slot_record.state,
                    gptid=slot_record.gptid,
                    persistent_id_label=slot_record.persistent_id_label,
                    disk_identity_key=slot_record.disk_identity_key,
                    logical_unit_id=slot_record.logical_unit_id,
                    sas_address=slot_record.sas_address,
                )
            )
        return samples

    async def _enumerate_scopes(
        self,
        *,
        force_inventory: bool = True,
        cached_root_only: bool = False,
    ) -> list[ScopeSnapshot]:
        root_started = time.perf_counter()
        root_snapshot = await self._fetch_inventory(force=force_inventory)
        self._record_collection_stage(
            "inventory.root",
            time.perf_counter() - root_started,
            inventory_forced=force_inventory,
            cached_root_only=cached_root_only,
        )
        systems = root_snapshot.get("systems") or []
        if not systems:
            systems = [
                {
                    "id": root_snapshot.get("selected_system_id") or "default",
                    "label": root_snapshot.get("selected_system_label"),
                }
            ]

        scopes: list[ScopeSnapshot] = []
        seen: set[tuple[str, str | None]] = set()
        if cached_root_only:
            root_system_id = normalize_text(root_snapshot.get("selected_system_id"))
            if not root_system_id and systems:
                root_system_id = normalize_text(systems[0].get("id"))
            root_system_id = root_system_id or "default"
            selected_enclosure_id = normalize_text(root_snapshot.get("selected_enclosure_id"))
            root_enclosure_label = normalize_text(root_snapshot.get("selected_enclosure_label"))
            if selected_enclosure_id:
                scopes.append(
                    ScopeSnapshot(
                        system_id=root_system_id,
                        system_label=normalize_text(root_snapshot.get("selected_system_label")),
                        enclosure_id=selected_enclosure_id,
                        enclosure_label=root_enclosure_label,
                        snapshot=root_snapshot,
                    )
                )
            else:
                scopes.append(
                    ScopeSnapshot(
                        system_id=root_system_id,
                        system_label=normalize_text(root_snapshot.get("selected_system_label")),
                        enclosure_id=None,
                        enclosure_label=None,
                        snapshot=root_snapshot,
                    )
                )
            self._record_collection_stage(
                "inventory.cached_root_scope",
                0.0,
                system_id=root_system_id,
                system_label=normalize_text(root_snapshot.get("selected_system_label")),
                enclosure_id=selected_enclosure_id,
                enclosure_label=root_enclosure_label,
                scope_count=len(scopes),
            )
            return scopes

        for system in systems:
            system_id = normalize_text(system.get("id"))
            if not system_id:
                continue
            system_started = time.perf_counter()
            system_scope_start = len(scopes)
            try:
                system_snapshot = await self._fetch_inventory(system_id=system_id, force=force_inventory)
            except Exception as exc:  # noqa: BLE001 - keep broad saved-fleet sweeps moving.
                logger.warning("Skipping history scope enumeration for %s: %s", system_id, exc)
                self._record_collection_stage(
                    "inventory.system_failed",
                    time.perf_counter() - system_started,
                    system_id=system_id,
                    inventory_forced=force_inventory,
                    error=str(exc),
                )
                continue
            enclosures = system_snapshot.get("enclosures") or []
            if not enclosures:
                scope_key = (system_id, None)
                if scope_key not in seen:
                    seen.add(scope_key)
                    scopes.append(
                        ScopeSnapshot(
                            system_id=system_id,
                            system_label=normalize_text(system_snapshot.get("selected_system_label")),
                            enclosure_id=None,
                            enclosure_label=None,
                            snapshot=system_snapshot,
                        )
                    )
                await self._append_storage_view_scopes(
                    scopes,
                    seen,
                    system_id=system_id,
                    system_snapshot=system_snapshot,
                    force_inventory=force_inventory,
                    system_started=system_started,
                )
                self._record_collection_stage(
                    "inventory.system",
                    time.perf_counter() - system_started,
                    system_id=system_id,
                    system_label=normalize_text(system_snapshot.get("selected_system_label")),
                    inventory_forced=force_inventory,
                    enclosure_count=0,
                    scope_count=len(scopes) - system_scope_start,
                )
                continue

            selected_enclosure_id = normalize_text(system_snapshot.get("selected_enclosure_id"))
            for enclosure in enclosures:
                enclosure_id = normalize_text(enclosure.get("id"))
                if not enclosure_id:
                    continue
                scope_key = (system_id, enclosure_id)
                if scope_key in seen:
                    continue
                seen.add(scope_key)
                if enclosure_id == selected_enclosure_id:
                    snapshot = system_snapshot
                else:
                    enclosure_started = time.perf_counter()
                    try:
                        snapshot = await self._fetch_inventory(
                            system_id=system_id,
                            enclosure_id=enclosure_id,
                            force=force_inventory,
                        )
                    except Exception as exc:  # noqa: BLE001 - preserve the rest of the full-fleet pass.
                        logger.warning(
                            "Skipping history scope enumeration for %s enclosure %s: %s",
                            system_id,
                            enclosure_id,
                            exc,
                        )
                        self._record_collection_stage(
                            "inventory.enclosure_failed",
                            time.perf_counter() - enclosure_started,
                            system_id=system_id,
                            enclosure_id=enclosure_id,
                            inventory_forced=force_inventory,
                            error=str(exc),
                        )
                        continue
                scopes.append(
                    ScopeSnapshot(
                        system_id=system_id,
                        system_label=normalize_text(snapshot.get("selected_system_label")),
                        enclosure_id=enclosure_id,
                        enclosure_label=normalize_text(snapshot.get("selected_enclosure_label")),
                        snapshot=snapshot,
                    )
                )
            await self._append_storage_view_scopes(
                scopes,
                seen,
                system_id=system_id,
                system_snapshot=system_snapshot,
                force_inventory=force_inventory,
                system_started=system_started,
            )
            self._record_collection_stage(
                "inventory.system",
                time.perf_counter() - system_started,
                system_id=system_id,
                system_label=normalize_text(system_snapshot.get("selected_system_label")),
                inventory_forced=force_inventory,
                enclosure_count=len(enclosures),
                scope_count=len(scopes) - system_scope_start,
            )
        return scopes

    async def _append_storage_view_scopes(
        self,
        scopes: list[ScopeSnapshot],
        seen: set[tuple[str, str | None]],
        *,
        system_id: str,
        system_snapshot: dict[str, Any],
        force_inventory: bool,
        system_started: float,
    ) -> None:
        try:
            storage_view_scopes = await self._enumerate_storage_view_scopes(
                system_id,
                system_snapshot,
                force_inventory=force_inventory,
            )
        except Exception as exc:  # noqa: BLE001 - storage views should not kill the whole sweep.
            logger.warning("Skipping history storage-view enumeration for %s: %s", system_id, exc)
            self._record_collection_stage(
                "storage_views.failed",
                time.perf_counter() - system_started,
                system_id=system_id,
                inventory_forced=force_inventory,
                error=str(exc),
            )
            return

        for scope in storage_view_scopes:
            scope_key = (scope.system_id, scope.enclosure_id)
            if scope_key in seen:
                continue
            seen.add(scope_key)
            scopes.append(scope)

    async def _fetch_inventory(
        self,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        *,
        force: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if force:
            params["force"] = "true"
        if system_id:
            params["system_id"] = system_id
        if enclosure_id:
            params["enclosure_id"] = enclosure_id
        return await self._fetch_json("/api/inventory", params=params)

    async def _fetch_storage_views(
        self,
        system_id: str,
        enclosure_id: str | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"system_id": system_id}
        if force:
            params["force"] = "true"
        if enclosure_id:
            params["enclosure_id"] = enclosure_id
        return await self._fetch_json("/api/storage-views", params=params)

    async def _enumerate_storage_view_scopes(
        self,
        system_id: str,
        system_snapshot: dict[str, Any],
        *,
        force_inventory: bool = False,
    ) -> list[ScopeSnapshot]:
        payload = await self._fetch_storage_views(system_id=system_id, force=force_inventory)
        system_label = normalize_text(payload.get("system_label")) or normalize_text(system_snapshot.get("selected_system_label"))
        platform = normalize_text(system_snapshot.get("selected_system_platform"))
        sources = system_snapshot.get("sources") if isinstance(system_snapshot.get("sources"), dict) else {}
        scopes: list[ScopeSnapshot] = []
        for view_payload in payload.get("views") or []:
            if not isinstance(view_payload, dict):
                continue
            if normalize_text(view_payload.get("source")) != "inventory_binding":
                continue
            view_id = normalize_text(view_payload.get("id"))
            view_label = normalize_text(view_payload.get("label"))
            if not view_id or not view_label:
                continue
            slot_payloads = []
            occupied_count = 0
            for slot_payload in view_payload.get("slots") or []:
                if not isinstance(slot_payload, dict):
                    continue
                try:
                    slot_index = int(slot_payload.get("slot_index"))
                except (TypeError, ValueError):
                    continue
                occupied = bool(slot_payload.get("occupied"))
                if occupied:
                    occupied_count += 1
                slot_payloads.append(
                    {
                        "slot": slot_index,
                        "slot_label": normalize_text(slot_payload.get("slot_label")) or f"{slot_index:02d}",
                        "enclosure_id": f"{STORAGE_VIEW_SCOPE_PREFIX}{view_id}",
                        "enclosure_label": view_label,
                        "present": occupied,
                        "state": normalize_text(slot_payload.get("state")) or ("matched" if occupied else "empty"),
                        "identify_active": False,
                        "device_name": normalize_text(slot_payload.get("device_name")),
                        "serial": normalize_text(slot_payload.get("serial")),
                        "model": normalize_text(slot_payload.get("model")),
                        "gptid": normalize_text(slot_payload.get("gptid")),
                        "persistent_id_label": normalize_text(slot_payload.get("persistent_id_label")),
                        "logical_unit_id": normalize_text(slot_payload.get("logical_unit_id")),
                        "sas_address": normalize_text(slot_payload.get("sas_address")),
                        "pool_name": normalize_text(slot_payload.get("pool_name")),
                        "vdev_name": None,
                        "health": normalize_text(slot_payload.get("health")),
                        "topology_label": normalize_text(slot_payload.get("description"))
                        or normalize_text(slot_payload.get("placement_key")),
                    }
                )
            if not slot_payloads or occupied_count == 0:
                continue
            scopes.append(
                ScopeSnapshot(
                    system_id=system_id,
                    system_label=system_label,
                    enclosure_id=f"{STORAGE_VIEW_SCOPE_PREFIX}{view_id}",
                    enclosure_label=view_label,
                    snapshot={
                        "selected_system_id": system_id,
                        "selected_system_label": system_label,
                        "selected_system_platform": platform,
                        "selected_enclosure_id": f"{STORAGE_VIEW_SCOPE_PREFIX}{view_id}",
                        "selected_enclosure_label": view_label,
                        "storage_view_id": view_id,
                        "storage_view_backing_enclosure_id": normalize_text(view_payload.get("backing_enclosure_id")),
                        "sources": sources,
                        "slots": slot_payloads,
                    },
                )
            )
        return scopes

    async def _fetch_smart_summaries(
        self,
        scope: ScopeSnapshot,
        slot_numbers: list[int],
        *,
        force_fresh: bool = False,
    ) -> dict[int, dict[str, Any]]:
        summaries: dict[int, dict[str, Any]] = {}
        storage_view_id = normalize_text(scope.snapshot.get("storage_view_id"))
        if storage_view_id:
            backing_enclosure_id = normalize_text(scope.snapshot.get("storage_view_backing_enclosure_id"))
            for slot_number in slot_numbers:
                params: dict[str, Any] = {
                    "system_id": scope.system_id,
                    "enclosure_id": backing_enclosure_id,
                }
                if force_fresh:
                    params["fresh"] = "true"
                payload = await self._fetch_json(
                    f"/api/storage-views/{urllib.parse.quote(storage_view_id)}/slots/{slot_number}/smart",
                    params=params,
                    timeout_seconds=self._smart_request_timeout_seconds(force_fresh=force_fresh),
                )
                if isinstance(payload, dict):
                    summaries[slot_number] = payload
            return summaries

        batch_size = max(1, self.settings.smart_batch_size)
        for offset in range(0, len(slot_numbers), batch_size):
            chunk = slot_numbers[offset : offset + batch_size]
            params: dict[str, Any] = {
                "system_id": scope.system_id,
                "enclosure_id": scope.enclosure_id,
            }
            if force_fresh:
                params["fresh"] = "true"
            payload = await self._fetch_json(
                "/api/slots/smart-batch",
                params=params,
                method="POST",
                body=json.dumps({"slots": chunk}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout_seconds=self._smart_request_timeout_seconds(force_fresh=force_fresh),
            )
            for item in payload.get("summaries", []):
                try:
                    slot_number = int(item.get("slot"))
                except (TypeError, ValueError):
                    continue
                summary = item.get("summary")
                if isinstance(summary, dict):
                    summaries[slot_number] = summary
        return summaries

    def _smart_request_timeout_seconds(self, *, force_fresh: bool) -> int | None:
        if force_fresh:
            return None
        return min(self.settings.request_timeout_seconds, CACHED_SMART_REQUEST_TIMEOUT_SECONDS)

    async def _fetch_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._fetch_json_sync,
            path,
            params or {},
            method,
            body,
            headers or {},
            timeout_seconds,
        )

    def _fetch_json_sync(
        self,
        path: str,
        params: dict[str, Any],
        method: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        filtered_params = {key: value for key, value in params.items() if value not in {None, ""}}
        query = urllib.parse.urlencode(filtered_params, doseq=True)
        url = f"{self.settings.source_base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=request_id_headers(headers),
        )
        request_timeout_seconds = timeout_seconds or self.settings.request_timeout_seconds
        try:
            with urllib.request.urlopen(request, timeout=request_timeout_seconds) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"{method} {url} timed out after {request_timeout_seconds}s") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {url} returned invalid JSON: {exc}") from exc

        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RuntimeError(str(payload.get("detail") or f"{method} {url} returned an application error."))
        if not isinstance(payload, dict):
            raise RuntimeError(f"{method} {url} returned a non-object JSON payload.")
        return payload

    @staticmethod
    def _interval_due(last_run_at: str | None, interval_seconds: int, now: datetime) -> bool:
        if last_run_at is None:
            return True
        try:
            previous = datetime.fromisoformat(last_run_at)
        except ValueError:
            return True
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        return now - previous >= timedelta(seconds=max(1, interval_seconds))
