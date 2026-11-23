"""Bounded, read-only observations of TrueNAS disk sync jobs and prerequisites."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.domain import DiskInventorySyncMode, DiskInventorySyncResult
from app.services.truenas_ws import TrueNASAPIError

DISK_INVENTORY_SYNC_MIDCLT_BY_PLATFORM = {"core": "/usr/local/bin/midclt", "scale": "/usr/bin/midclt"}
DISK_INVENTORY_SYNC_JOB_STATES = frozenset({"WAITING", "RUNNING", "SUCCESS", "FAILED", "ABORTED"})
DISK_INVENTORY_SYNC_TERMINAL_JOB_STATES = frozenset({"SUCCESS", "FAILED", "ABORTED"})
DISK_INVENTORY_SYNC_ERROR_MAX_CHARS = 400


class DiskInventorySyncUnavailable(TrueNASAPIError):
    """The requested disk inventory sync mode cannot run on this system."""


class DiskInventorySyncBusy(TrueNASAPIError):
    """A known job or a local request already owns this sync operation."""

    def __init__(self, message: str, *, result: DiskInventorySyncResult | None = None):
        super().__init__(message)
        self.result = result


@dataclass(slots=True)
class _DiskInventorySyncState:
    """Shared intact with a settings-reload successor; bounded to one observation."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    multipath_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_job_id: int | None = None
    observation: dict[str, Any] | None = None


def _bounded_middleware_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())
    if not text:
        return None
    if len(text) > DISK_INVENTORY_SYNC_ERROR_MAX_CHARS:
        return text[: DISK_INVENTORY_SYNC_ERROR_MAX_CHARS - 3].rstrip() + "..."
    return text


def _parse_disk_inventory_sync_job_id(stdout: str) -> int | None:
    text = (stdout or "").strip()
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _job_entry(stdout: str, job_id: int) -> dict[str, Any]:
    try:
        payload = json.loads((stdout or "").strip() or "null")
    except ValueError as exc:
        raise TrueNASAPIError(f"TrueNAS returned an unreadable status for disk sync job {job_id}.") from exc
    entries = payload if isinstance(payload, list) else [payload]
    entry = next((row for row in entries if isinstance(row, dict) and type(row.get("id")) is int and row["id"] == job_id), None)
    if entry is None:
        raise TrueNASAPIError(f"TrueNAS no longer lists disk sync job {job_id}.")
    return entry


def _job_state(entry: dict[str, Any]) -> str:
    raw = entry.get("state")
    state = raw.strip().upper() if isinstance(raw, str) else "UNKNOWN"
    return state if state in DISK_INVENTORY_SYNC_JOB_STATES else "UNKNOWN"


def _parse_disk_inventory_sync_job(stdout: str, job_id: int) -> tuple[str, str | None]:
    entry = _job_entry(stdout, job_id)
    return _job_state(entry), _bounded_middleware_text(entry.get("error"))


def _job_details(entry: dict[str, Any]) -> str:
    parts = [_job_state(entry).lower()]
    progress = entry.get("progress")
    if isinstance(progress, dict):
        percent = progress.get("percent")
        if type(percent) in (int, float) and math.isfinite(percent) and 0 <= percent <= 100:
            parts.append(f"{percent:g}%")
        description = _bounded_middleware_text(progress.get("description"))
        if description:
            parts.append(description)
    started = entry.get("time_started")
    try:
        if isinstance(started, dict):
            stamp = float(started["$date"]) / 1000
        elif isinstance(started, str):
            stamp = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
        else:
            stamp = None
        if stamp is not None and math.isfinite(stamp):
            elapsed = max(0, int(time.time() - stamp))
            parts.append(f"{elapsed // 60} min" if elapsed >= 60 else f"{elapsed} s")
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        pass
    return ", ".join(parts)


def multipath_prerequisite(disks: list[dict[str, Any]], parsed: Any) -> str | None:
    """Require exact serial AND LUN agreement, never guessed path equivalence."""
    covered = {consumer.device_name.removeprefix("/dev/").lower()
               for geom in parsed.multipath_info.values() for consumer in geom.consumers}
    groups: dict[tuple[str, str], set[str]] = {}
    for row in disks:
        device = str(row.get("name") or row.get("devname") or "").removeprefix("/dev/").lower()
        serial, lunid = row.get("serial"), row.get("lunid")
        if not re.fullmatch(r"da[0-9]+", device) or not isinstance(serial, str) or not isinstance(lunid, str):
            continue
        serial, lunid = serial.strip(), lunid.strip()
        if not serial or not lunid or row.get("multipath_name") or device in covered:
            continue
        groups.setdefault((serial, lunid), set()).add(device)
    paths = sorted({device for devices in groups.values() if len(devices) > 1 for device in devices})
    if not paths:
        return None
    shown = ", ".join(paths[:8]) + (", ..." if len(paths) > 8 else "")
    return (f"Collected CORE inventory shows several disk paths ({shown}) with the same serial and LUN id "
            "and no multipath provider. Run 'Sync multipath table', then refresh before 'Full disk sync'. "
            "No sync was started.")


class DiskSyncStatusMixin:
    """Status reads never start jobs and unreadable status never means FAILED."""

    async def _read_disk_sync_job(self, midclt: str, job_id: int, *, budget: float) -> tuple[str, str | None]:
        deadline = self._disk_inventory_sync_clock() + max(0.0, budget)
        self._disk_inventory_sync_state.observation = None
        for attempt in range(3):
            remaining = deadline - self._disk_inventory_sync_clock()
            if remaining <= 0:
                break
            # Reserve the remaining retries and backoff inside this read budget.
            backoff = (0.5, 1.0, 0.0)[attempt]
            timeout = max(0.01, (remaining - sum((0.5, 1.0)[attempt:])) / (3 - attempt))
            try:
                job_filter = json.dumps([["id", "=", job_id]], separators=(",", ":"))
                async with asyncio.timeout(min(remaining, timeout)):
                    result = await self._run_disk_inventory_sync_command(
                        [midclt, "call", "core.get_jobs", job_filter],
                        failure_prefix=f"TrueNAS could not report disk sync job {job_id}",
                        timeout_seconds=min(remaining, timeout),
                    )
                entry = _job_entry(result.stdout, job_id)
                self._disk_inventory_sync_state.observation = entry
                return _job_state(entry), _bounded_middleware_text(entry.get("error"))
            except (TrueNASAPIError, TimeoutError, OSError):
                if attempt < 2 and deadline - self._disk_inventory_sync_clock() > backoff:
                    await self._disk_inventory_sync_sleep(backoff)
        return "UNKNOWN", None

    async def _disk_sync_status_result(self, midclt: str, job_id: int, *, budget: float) -> DiskInventorySyncResult:
        started = self._disk_inventory_sync_clock()
        state, error = await self._read_disk_sync_job(midclt, job_id, budget=budget)
        entry = self._disk_inventory_sync_state.observation or {"state": state}
        if state == "WAITING":
            message = f"TrueNAS disk sync job {job_id} is waiting in the disk.sync_all queue."
            try:
                remaining = budget - (self._disk_inventory_sync_clock() - started)
                if remaining > 0:
                    async with asyncio.timeout(min(3.0, remaining)):
                        jobs = await self.truenas_client.fetch_disk_sync_jobs()
                    blockers = [row for row in jobs if isinstance(row, dict)
                                and type(row.get("id")) is int and row["id"] < job_id
                                and row.get("method") == "disk.sync_all"
                                and _job_state(row) in {"RUNNING", "WAITING"}]
                    blockers.sort(key=lambda row: (_job_state(row) != "RUNNING", row["id"]))
                    if blockers:
                        blocker = blockers[0]
                        message = f"Waiting behind disk sync job {blocker['id']} ({_job_details(blocker)}). Our job {job_id} is queued."
            except (TrueNASAPIError, TimeoutError, OSError, TypeError):
                # Optional queue context cannot change the known job state.
                pass
        elif state == "RUNNING":
            message = f"TrueNAS disk sync job {job_id} is still running ({_job_details(entry)})."
        elif state == "UNKNOWN":
            message = f"The previous disk sync job {job_id}'s state is unknown. You can start a new full sync; TrueNAS will queue it."
        elif state == "SUCCESS":
            message = "TrueNAS re-read its disk inventory. Refresh to see the updated bays."
        elif state == "ABORTED":
            message = f"TrueNAS aborted disk sync job {job_id} before it finished."
        else:
            message = f"TrueNAS reported disk sync job {job_id} failed."
        return DiskInventorySyncResult(mode=DiskInventorySyncMode.full, state=state, job_id=job_id,
                                       elapsed_seconds=self._disk_inventory_sync_elapsed(started), error=error, message=message)

    async def get_disk_inventory_sync_status(self, job_id: int | None = None) -> DiskInventorySyncResult:
        reason = self.disk_inventory_sync_unavailable_reason(DiskInventorySyncMode.full)
        if reason:
            raise DiskInventorySyncUnavailable(reason)
        job_id = job_id if job_id is not None else self._disk_inventory_sync_active_job_id
        if type(job_id) is not int or job_id < 0:
            return DiskInventorySyncResult(mode=DiskInventorySyncMode.full, state="UNKNOWN", message="No disk sync job is being tracked.")
        midclt = DISK_INVENTORY_SYNC_MIDCLT_BY_PLATFORM[self.system.truenas.platform]
        result = await self._disk_sync_status_result(midclt, job_id, budget=min(50.0, max(1, self.settings.app.disk_inventory_sync_timeout_seconds)))
        if result.state in DISK_INVENTORY_SYNC_TERMINAL_JOB_STATES:
            if self._disk_inventory_sync_active_job_id == job_id:
                self._disk_inventory_sync_active_job_id = None
            if result.state == "SUCCESS":
                self.invalidate_snapshot_cache(reason="disk_inventory_sync.full", invalidate_source_bundle=True)
        return result

    def _disk_sync_multipath_prerequisite(self) -> str | None:
        bundle = self._source_bundle
        if self.system.truenas.platform != "core" or bundle is None or not bundle.ssh_collected:
            return None
        parsed = self._parsed_ssh_data_for_enclosure(bundle, None)
        return multipath_prerequisite(bundle.raw_data.disks, parsed)
