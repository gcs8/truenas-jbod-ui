from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from history_service.config import HistorySettings
from history_service.domain import (
    MetricSample,
    SlotStateRecord,
    build_slot_events,
    isoformat_utc,
    normalize_text,
    utcnow,
)
from history_service.store import HistoryStore

logger = logging.getLogger(__name__)

FAST_METRIC_FIELDS = ("temperature_c",)
SLOW_METRIC_FIELDS = (
    "bytes_read",
    "bytes_written",
    "annualized_bytes_written",
    "power_on_hours",
)
EXTENDED_STATE_FIELDS = (
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


@dataclass(slots=True, frozen=True)
class ScopeSnapshot:
    system_id: str
    system_label: str | None
    enclosure_id: str | None
    enclosure_label: str | None
    snapshot: dict[str, Any]


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
        self.last_error: str | None = None
        self.last_scope_count: int = 0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name="history-collector")

    async def stop(self) -> None:
        if not self._task:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_once(self) -> None:
        run_started = utcnow()
        observed_at = isoformat_utc(run_started)
        scopes = await self._enumerate_scopes()
        self.last_scope_count = len(scopes)
        self.last_inventory_at = observed_at
        collect_fast = self._interval_due(
            self.last_fast_metrics_at,
            self.settings.fast_interval_seconds,
            run_started,
        )
        collect_slow = self._interval_due(
            self.last_slow_metrics_at,
            self.settings.slow_interval_seconds,
            run_started,
        )

        for scope in scopes:
            slot_records = [
                SlotStateRecord.from_snapshot_slot(scope.snapshot, slot_payload)
                for slot_payload in scope.snapshot.get("slots", [])
            ]
            self._record_slot_changes(slot_records, observed_at)

            if not collect_fast and not collect_slow:
                continue

            present_slots = [record.slot for record in slot_records if record.present]
            if not present_slots:
                continue
            summaries = await self._fetch_smart_summaries(scope, present_slots)
            metric_samples: list[MetricSample] = []
            for record in slot_records:
                summary = summaries.get(record.slot)
                if not isinstance(summary, dict):
                    continue
                if collect_fast:
                    metric_samples.extend(
                        self._build_metric_samples(record, summary, observed_at, FAST_METRIC_FIELDS)
                    )
                if collect_slow:
                    metric_samples.extend(
                        self._build_metric_samples(record, summary, observed_at, SLOW_METRIC_FIELDS)
                    )
            self.store.insert_metric_samples(metric_samples)

        if collect_fast:
            self.last_fast_metrics_at = observed_at
        if collect_slow:
            self.last_slow_metrics_at = observed_at
            try:
                backup_path = self.store.create_backup(
                    self.settings.backup_dir,
                    snapshot_label=observed_at,
                    retention_count=self.settings.backup_retention_count,
                )
                if backup_path:
                    self.last_backup_at = observed_at
            except Exception as exc:  # noqa: BLE001 - keep collection alive even if backup snapshotting fails.
                logger.warning("History backup snapshot failed: %s", exc)
        self.last_success_at = observed_at
        self.last_error = None

    def status(self) -> dict[str, Any]:
        return {
            "collector_running": bool(self._task and not self._task.done()),
            "started_at": self.started_at,
            "last_inventory_at": self.last_inventory_at,
            "last_fast_metrics_at": self.last_fast_metrics_at,
            "last_slow_metrics_at": self.last_slow_metrics_at,
            "last_success_at": self.last_success_at,
            "last_backup_at": self.last_backup_at,
            "last_error": self.last_error,
            "last_scope_count": self.last_scope_count,
            "source_base_url": self.settings.source_base_url,
            "sqlite_path": self.settings.sqlite_path,
        }

    async def _run_loop(self) -> None:
        if self.settings.startup_grace_seconds > 0:
            await asyncio.sleep(self.settings.startup_grace_seconds)

        while not self._stopping.is_set():
            started = utcnow()
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the collector alive across transient appliance errors.
                logger.exception("History collection pass failed")
                self.last_error = str(exc)

            elapsed = (utcnow() - started).total_seconds()
            sleep_for = max(1.0, self.settings.poll_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                continue

    def _record_slot_changes(self, slot_records: list[SlotStateRecord], observed_at: str) -> None:
        for record in slot_records:
            previous = self.store.get_slot_state(record.system_id, record.enclosure_id, record.slot)
            if self._should_backfill_extended_state(previous, record):
                self.store.upsert_slot_state(record, observed_at)
                continue
            events = build_slot_events(previous, record, observed_at)
            self.store.insert_events(events)
            self.store.upsert_slot_state(record, observed_at)

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
                )
            )
        return samples

    async def _enumerate_scopes(self) -> list[ScopeSnapshot]:
        root_snapshot = await self._fetch_inventory()
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
        for system in systems:
            system_id = normalize_text(system.get("id"))
            if not system_id:
                continue
            system_snapshot = await self._fetch_inventory(system_id=system_id)
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
                snapshot = (
                    system_snapshot
                    if enclosure_id == selected_enclosure_id
                    else await self._fetch_inventory(system_id=system_id, enclosure_id=enclosure_id)
                )
                scopes.append(
                    ScopeSnapshot(
                        system_id=system_id,
                        system_label=normalize_text(snapshot.get("selected_system_label")),
                        enclosure_id=enclosure_id,
                        enclosure_label=normalize_text(snapshot.get("selected_enclosure_label")),
                        snapshot=snapshot,
                    )
                )
        return scopes

    async def _fetch_inventory(
        self,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"force": "true"}
        if system_id:
            params["system_id"] = system_id
        if enclosure_id:
            params["enclosure_id"] = enclosure_id
        return await self._fetch_json("/api/inventory", params=params)

    async def _fetch_smart_summaries(
        self,
        scope: ScopeSnapshot,
        slot_numbers: list[int],
    ) -> dict[int, dict[str, Any]]:
        summaries: dict[int, dict[str, Any]] = {}
        batch_size = max(1, self.settings.smart_batch_size)
        for offset in range(0, len(slot_numbers), batch_size):
            chunk = slot_numbers[offset : offset + batch_size]
            payload = await self._fetch_json(
                "/api/slots/smart-batch",
                params={
                    "system_id": scope.system_id,
                    "enclosure_id": scope.enclosure_id,
                },
                method="POST",
                body=json.dumps({"slots": chunk}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
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

    async def _fetch_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._fetch_json_sync,
            path,
            params or {},
            method,
            body,
            headers or {},
        )

    def _fetch_json_sync(
        self,
        path: str,
        params: dict[str, Any],
        method: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        filtered_params = {key: value for key, value in params.items() if value not in {None, ""}}
        query = urllib.parse.urlencode(filtered_params, doseq=True)
        url = f"{self.settings.source_base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.settings.request_timeout_seconds) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

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
