"""Backup scheduler core: build, catalog, ship and groom backup archives.

One process owns every write here (the ``enclosure-backup-scheduler`` sidecar):

* **config** backups are driven by the change journal (``ConfigBackupCoalescer``):
  a burst of config edits becomes one backup, and a burst that nets out to no
  change becomes nothing.
* **full** backups run on a cron schedule.

Both build their archive with the existing ``ScheduledBackupRunner`` (encrypted,
preflighted), catalog it in ``ArtifactCatalog`` with its change ids, copy it to
every enabled remote target, then plan and apply grooming with
``LifecycleManager``. A single job lock makes backups and grooming single-flight.

A small, secret-free status file (``BACKUP_ARCHIVE_STATUS_FILE``) records the
last result per class and per target; the main UI reads it for ``/healthz``.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import local as _ThreadLocal
from types import MappingProxyType
from typing import Any

import yaml

from history_service.backup_archive.catalog import (
    LOCAL_LOCATION,
    ArtifactCatalog,
    ArtifactRecord,
    new_artifact_id,
    validate_record,
)
from history_service.backup_archive.cron import CronSchedule
from history_service.backup_archive.errors import describe_error as _describe_error
from history_service.backup_archive.journal import ChangeJournal, ConfigBackupCoalescer, CoalescerResult
from history_service.backup_archive.lifecycle import (
    GroomableTarget,
    GroomingPlan,
    KeeperCheckUnavailable,
    LifecycleManager,
    LocalBackupDirectory,
    PayloadDigest,
    RetentionRule,
    local_payload_digest,
    local_target_resolver,
)
from history_service.backup_archive.policy import (
    ArchiveTarget,
    BackupPolicy,
    validate_filesystem_target_roots,
)
from history_service.backup_archive.settings import PROVIDERS
from history_service.backup_archive.transport import (
    ArchiveDownloadTooLargeError,
    ArchivePublicationUncertainError,
    LocalDirectoryTarget,
    StoredObject,
    open_target,
    transport_encrypted,
)
from history_service.scheduled_backup import ScheduledBackupRunner

logger = logging.getLogger(__name__)

STATUS_SCHEMA_VERSION = 1
STATUS_FILE_MODE = 0o640
SHARED_STATUS_DIRECTORY_MODE = 0o2750
PLAN_TTL_SECONDS = 600
MAX_PLANS = 16
MAX_DETAIL_CHARS = 300
BACKUP_CLASSES = ("config", "full")
HISTORY_GROUP = "history_db"
HISTORY_REPLACEMENT_COPIES = 14
# Grooming's remote keeper readback stages one copy under the state directory
# (#1188). It is refused unless the copy plus this much stays free there.
KEEPER_SCRATCH_PREFIX = "keeper-check-"
# Local FULL reads made by grooming's keeper check in one run, by artifact id:
# (size, sha256), or None for a read that did not complete.
_LocalReads = dict[str, tuple[int, str] | None]
KEEPER_SCRATCH_HEADROOM_BYTES = 64 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
# Marks the synthetic config run recorded when the change journal cannot be read.
JOURNAL_CHECK_PREFIX = "Config changes could not be checked: "
# Trust-on-first-use SFTP targets pin new host keys here, in the scheduler's
# private state, never in a file a target names (#816).
SFTP_PIN_FILE_NAME = "sftp_known_hosts"


class SchedulerBusyError(RuntimeError):
    """Another backup or grooming run holds the job lock."""


class ArtifactNotFoundError(LookupError):
    pass


class ArchiveIntegrityError(RuntimeError):
    """A copy's bytes do not match its catalogued size and SHA-256."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def describe_error(exc: BaseException) -> str:
    """Short operator-facing text without raw provider data (#737)."""

    return _describe_error(exc, MAX_DETAIL_CHARS)


def config_group_keys(all_backup_groups: tuple[str, ...], history_group: str) -> list[str]:
    """Config class = every default backup group except the history database."""

    return [key for key in all_backup_groups if key != history_group]


def _is_journal_check_record(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("ok") is False
        and isinstance(record.get("detail"), str)
        and record["detail"].startswith(JOURNAL_CHECK_PREFIX)
    )


@dataclass
class RunRecord:
    at: str
    ok: bool
    detail: str | None = None
    artifact_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "ok": self.ok, "detail": self.detail, "artifact_id": self.artifact_id}


@dataclass
class SchedulerPaths:
    local_dir: Path            # local archive root (the ``local`` location)
    state_dir: Path            # private: catalog + artifact metadata
    journal_path: Path         # shared config-change journal
    status_file: Path | None   # secret-free status for /healthz (shared status dir)
    passphrase_file: Path
    runner_status_dir: Path    # shared status dir for ScheduledBackupRunner status files
    full_status_file: Path | None = None  # e.g. SCHEDULED_BACKUP_STATUS_FILE, keeps segment retention fed
    history_backup_dir: Path | None = None
    history_long_term_backup_dir: Path | None = None
    history_database_stem: str = "history"


@dataclass
class _PlanEntry:
    expires_at: float
    plan: GroomingPlan


class BackupScheduler:
    def __init__(
        self,
        policy: BackupPolicy,
        backup_service: Any,
        paths: SchedulerPaths,
        *,
        app_gid: int,
        config_groups: list[str],
        full_groups: list[str],
        snapshot_config: Callable[[], Mapping[str, Any]],
        open_target_fn: Callable[..., Any] = open_target,
        runner_factory: Callable[..., Any] = ScheduledBackupRunner,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        local_tz: Any = None,
    ) -> None:
        # Policy loading normally performs this check, but the scheduler is the
        # final authority because its local root may be injected directly and
        # filesystem aliases may have changed since the config was parsed. A
        # target that cannot be inspected right now must not stop local
        # backups; the per-operation check below refuses it until it can.
        validate_filesystem_target_roots(
            policy.enabled_targets(),
            paths.local_dir,
            allow_unavailable=True,
        )
        self.policy = policy
        self.backup_service = backup_service
        self.paths = paths
        self.app_gid = int(app_gid)
        self.groups = {"config": list(config_groups), "full": list(full_groups)}
        self._open_target = open_target_fn
        self._open_target_is_default = open_target_fn is open_target
        self._runner_factory = runner_factory
        self._clock = clock
        self._monotonic = monotonic
        self._local_tz = local_tz
        self._job_lock = threading.Lock()
        self._job_owner = _ThreadLocal()
        self._state_lock = threading.RLock()
        self._running: dict[str, str] | None = None
        self._plans: dict[str, _PlanEntry] = {}
        self._status: dict[str, Any] = {"classes": {}, "targets": {}}
        paths.local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.catalog = ArtifactCatalog(paths.state_dir / "catalog.sqlite3")
        self._meta_path = paths.state_dir / "artifact-meta.json"
        try:
            self._meta: dict[str, dict[str, Any]] = self._load_meta()
            self._recover_publications()
        except BaseException:
            self.catalog.close()
            raise
        self._load_status()
        self.journal: ChangeJournal | None = None
        self.coalescer: ConfigBackupCoalescer | None = None
        self._coalescer_error: str | None = None
        self._coalescer_prior_run: dict[str, Any] | None = None
        stored = self._status["classes"].get("config")
        if _is_journal_check_record(stored):
            # A check failure restored from the status file is never the last real run.
            self._coalescer_error = stored["detail"]
        if policy.config.enabled:
            self.journal = ChangeJournal(paths.journal_path, file_mode=0o660)
            self.coalescer = ConfigBackupCoalescer(
                self.journal,
                snapshot_config=snapshot_config,
                make_backup=lambda change_ids, snapshot: self._run_class_locked_or_busy("config", change_ids, snapshot),
                clock=monotonic,
                quiet_period=float(policy.config.debounce_seconds),
                max_delay=float(policy.config.max_delay_seconds),
            )
        self._cron = CronSchedule.parse(policy.full.schedule) if policy.full.enabled else None
        self.next_full_at: datetime | None = self._compute_next_full(self._clock())

    # -- metadata and status persistence -----------------------------------------------

    def _load_meta(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        # This file also holds publication ownership. Corruption or unreadable
        # state must not turn pending generations into apparently foreign files.
        if not isinstance(payload, dict) or any(not isinstance(meta, dict) for meta in payload.values()):
            raise ValueError("Backup artifact metadata is invalid.")
        return payload

    def _metadata_snapshot(self, artifact_id: str | None = None) -> dict[str, Any]:
        # Nested groups, verification results and publication identities must not
        # remain aliases into live state after the lock is released.
        with self._state_lock:
            metadata = self._meta if artifact_id is None else self._meta.get(artifact_id) or {}
            return copy.deepcopy(metadata)

    def _save_meta(self, metadata: dict[str, dict[str, Any]] | None = None) -> None:
        # Serialize snapshots through rename and directory durability, so a
        # concurrent verification cannot replace a newer ownership intent.
        with self._state_lock:
            payload = self._meta if metadata is None else metadata
            temporary = self._meta_path.with_name(f".{self._meta_path.name}.{uuid.uuid4().hex}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                    written = os.fstat(handle.fileno())
                try:
                    os.replace(temporary, self._meta_path)
                except BaseException:
                    # A failed acknowledgement can follow a completed rename.
                    # Retain ownership if installed, or if inspection cannot
                    # rule it out. Never overwrite that intent with an old view.
                    try:
                        installed = self._meta_path.lstat()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        self._meta = payload
                    else:
                        if (installed.st_dev, installed.st_ino) == (written.st_dev, written.st_ino):
                            self._meta = payload
                    raise
                self._meta = payload
                descriptor = os.open(self._meta_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)

    def _remember_publication(
        self, artifact_id: str, backup_class: str, created_at: datetime,
        change_ids: tuple[str, ...], temporary: Path, target: Path,
    ) -> None:
        """Reuse private artifact metadata as a pre-publication ownership intent."""
        metadata = temporary.lstat()
        parent = target.parent.stat()
        size, digest = _hash_file(temporary)
        artifact_meta = {
            "publication": {
                "backup_class": backup_class,
                "name": f"{backup_class}/{target.name}",
                "created_at": _iso(created_at),
                "size": size,
                "sha256": digest,
                "change_ids": list(change_ids),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "parent_device": parent.st_dev,
                "parent_inode": parent.st_ino,
                "temporary_name": temporary.name,
            },
            "history_replacement": False,
        }
        with self._state_lock:
            # Stage the new intent without exposing it to later unrelated saves
            # when serialization, file durability or replacement fails. Once
            # replaced, _save_meta keeps it even if directory durability fails.
            self._save_meta({**self._meta, artifact_id: artifact_meta})

    def _pending_publications(self, metadata: Mapping[str, Any] | None = None) -> list[ArtifactRecord]:
        if metadata is None:
            metadata = self._metadata_snapshot()
        records = []
        for artifact_id, meta in metadata.items():
            intent = meta.get("publication")
            if intent is None:
                continue
            try:
                record = validate_record(ArtifactRecord(
                    artifact_id=artifact_id, backup_class=intent["backup_class"],
                    location=LOCAL_LOCATION, name=intent["name"],
                    created_at=datetime.fromisoformat(intent["created_at"]),
                    size=intent["size"], sha256=intent["sha256"], verified=False,
                    change_ids=tuple(intent["change_ids"]), preserved=True,
                    preserve_reason="Interrupted publication; verify before releasing preservation.",
                    preserved_by="scheduler-recovery",
                ))
                if record.name.split("/")[:1] != [record.backup_class] or len(record.name.split("/")) != 2:
                    raise ValueError("Invalid publication path")
                for key in ("device", "inode", "parent_device", "parent_inode"):
                    if type(intent[key]) is not int or intent[key] < 0:
                        raise ValueError("Invalid publication identity")
                temporary_name = intent.get("temporary_name")
                target_name = record.name.split("/", 1)[1]
                if (
                    temporary_name is not None
                    and (
                        not isinstance(temporary_name, str)
                        or re.fullmatch(
                            rf"\.{re.escape(target_name)}\.[0-9a-f]{{32}}\.tmp",
                            temporary_name,
                        )
                        is None
                    )
                ):
                    raise ValueError("Invalid publication temporary name")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Backup publication ownership metadata is invalid.") from exc
            records.append(record)
        return records

    def _publication_descriptor(
        self,
        descriptor: int,
        record: ArtifactRecord,
        intent: Mapping[str, Any],
    ) -> os.stat_result | None:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino) != (intent["device"], intent["inode"])
            or metadata.st_size != record.size
        ):
            return None
        digest = hashlib.sha256()
        remaining = record.size
        while remaining:
            chunk = os.read(descriptor, min(_COPY_CHUNK, remaining))
            if not chunk:
                return None
            remaining -= len(chunk)
            digest.update(chunk)
        if os.read(descriptor, 1) or digest.hexdigest() != record.sha256:
            return None
        if self._snapshot_identity(os.fstat(descriptor)) != self._snapshot_identity(metadata):
            return None
        return metadata

    def _publication_state(self, record: ArtifactRecord, intent: Mapping[str, Any]) -> str:
        path = self._local_path(record)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(directory)
            if (parent.st_dev, parent.st_ino) != (intent["parent_device"], intent["parent_inode"]):
                return "mismatch"
            temporary_name = intent.get("temporary_name")
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            except FileNotFoundError:
                if temporary_name is None:
                    return "missing"
                try:
                    temporary = os.open(
                        temporary_name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                except FileNotFoundError:
                    return "missing"
                try:
                    temporary_metadata = self._publication_descriptor(temporary, record, intent)
                    if temporary_metadata is None or temporary_metadata.st_nlink != 1:
                        return "mismatch"
                    if (
                        self._snapshot_identity(
                            os.stat(temporary_name, dir_fd=directory, follow_symlinks=False)
                        )
                        != self._snapshot_identity(temporary_metadata)
                    ):
                        return "mismatch"
                finally:
                    os.close(temporary)
                os.unlink(temporary_name, dir_fd=directory)
                os.fsync(directory)
                return "missing"
            try:
                metadata = self._publication_descriptor(descriptor, record, intent)
                if metadata is None:
                    return "mismatch"
                if (
                    self._snapshot_identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False))
                    != self._snapshot_identity(metadata)
                ):
                    return "mismatch"
                if metadata.st_nlink == 1:
                    if temporary_name is not None:
                        try:
                            os.stat(temporary_name, dir_fd=directory, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            return "mismatch"
                    return "match"
                if metadata.st_nlink != 2 or temporary_name is None:
                    return "mismatch"
                temporary_metadata = os.stat(
                    temporary_name,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
                if self._snapshot_identity(temporary_metadata) != self._snapshot_identity(metadata):
                    return "mismatch"
                os.unlink(temporary_name, dir_fd=directory)
                os.fsync(directory)
                published = os.fstat(descriptor)
                if (
                    published.st_nlink != 1
                    or self._snapshot_identity(
                        os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    )
                    != self._snapshot_identity(published)
                ):
                    return "mismatch"
                return "match"
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)

    def _recover_publications(self) -> None:
        # Only exact intents are considered. Never enumerate or adopt files by
        # filename. Recovered copies remain pinned and unverified, so interrupted
        # directory barriers cannot supply replacement/retention credit.
        metadata = self._metadata_snapshot()
        pending = self._pending_publications(metadata)
        if not pending:
            return
        deleted = {item.record.artifact_id for item in self.catalog.tombstones(location=LOCAL_LOCATION)}
        for record in pending:
            intent = metadata[record.artifact_id]["publication"]
            try:
                if self.catalog.get(record.artifact_id) is None and record.artifact_id not in deleted:
                    state = self._publication_state(record, intent)
                    if state == "mismatch":
                        continue
                    if state == "match":
                        self.catalog.add(record)
                with self._state_lock:
                    self._meta[record.artifact_id].pop("publication")
                    try:
                        self._save_meta()
                    except (OSError, ValueError, sqlite3.Error):
                        # Restore under the same lock if clearing did not persist.
                        # A catalog record or tombstone still wins on startup.
                        self._meta[record.artifact_id]["publication"] = intent
                        raise
            except (OSError, ValueError, sqlite3.Error) as exc:
                logger.warning("Backup publication recovery remains pending (%s).", type(exc).__name__)

    def _load_status(self) -> None:
        if self.paths.status_file is None:
            return
        payload = read_archive_status(self.paths.status_file)
        if payload:
            self._status["classes"] = dict(payload.get("classes") or {})
            self._status["targets"] = dict(payload.get("targets") or {})
            if isinstance(payload.get("grooming"), dict):
                self._status["grooming"] = payload["grooming"]
            persisted_receipt = payload.get("verified_full")
            verified_full = self._validated_verified_full_receipt(persisted_receipt)
            if verified_full is not None:
                self._status["verified_full"] = verified_full
            elif "verified_full" in payload:
                # The shared receipt is consumed independently by history. Keep
                # its on-disk view synchronized with the catalog during startup
                # instead of merely rejecting stale evidence in memory.
                self._write_status()

    def _validated_verified_full_receipt(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        artifact_id = value.get("artifact_id")
        created_at = value.get("created_at")
        groups = value.get("included_groups")
        if (
            not isinstance(artifact_id, str)
            or not isinstance(created_at, str)
            or not isinstance(groups, list)
            or HISTORY_GROUP not in groups
            or any(not isinstance(group, str) or not group for group in groups)
            or len(groups) != len(set(groups))
        ):
            return None
        record = self.catalog.get(artifact_id)
        meta = self._metadata_snapshot(artifact_id)
        if (
            record is None
            or record.backup_class != "full"
            or record.location != LOCAL_LOCATION
            or not record.verified
            or self._artifact_state(record, meta) != "ok"
            or meta.get("history_replacement") is not True
            or _iso(record.created_at) != created_at
        ):
            return None
        return {
            "artifact_id": artifact_id,
            "created_at": created_at,
            "included_groups": list(groups),
        }

    def _write_status(self) -> None:
        path = self.paths.status_file
        if path is None:
            return
        with self._state_lock:
            payload = {
                "schema_version": STATUS_SCHEMA_VERSION,
                "updated_at": _iso(self._clock()),
                "classes": self._status["classes"],
                "targets": {
                    target.target_id: {
                        **(self._status["targets"].get(target.target_id) or {}),
                        "label": target.label,
                    }
                    for target in self.policy.enabled_targets()
                    if self._status["targets"].get(target.target_id)
                },
            }
            if self._status.get("grooming"):
                payload["grooming"] = self._status["grooming"]
            verified_full = self._validated_verified_full_receipt(
                self._status.get("verified_full")
            )
            if verified_full is not None:
                payload["verified_full"] = verified_full
            else:
                self._status.pop("verified_full", None)
        try:
            write_shared_status(path, payload, app_gid=self.app_gid)
        except Exception as exc:  # noqa: BLE001 - status is advisory; never fail a backup on it
            logger.warning("Backup archive status could not be written (%s).", type(exc).__name__)

    def _record_class(self, backup_class: str, record: RunRecord) -> None:
        with self._state_lock:
            self._status["classes"][backup_class] = record.as_dict()
        self._write_status()

    def _record_target(self, target_id: str, record: RunRecord) -> None:
        with self._state_lock:
            self._status["targets"][target_id] = record.as_dict()

    # -- scheduling --------------------------------------------------------------------

    def _compute_next_full(self, now: datetime) -> datetime | None:
        if self._cron is None:
            return None
        local_now = now.astimezone(self._local_tz) if self._local_tz is not None else now.astimezone()
        following = self._cron.next_after(local_now)
        while True:
            following_utc = following.astimezone(timezone.utc)
            round_trip = following_utc.astimezone(following.tzinfo)
            # Reject a wall-clock time that the zone skips during spring-forward.
            # Inside a repeated fall-back hour, also skip the first occurrence
            # once it is already past so a repeated time runs only once.
            if (
                round_trip.replace(tzinfo=None) == following.replace(tzinfo=None)
                and following_utc > now
            ):
                return following_utc
            following = self._cron.next_after(following)

    def tick(self) -> None:
        """One scheduler step: config coalescer, then a due full backup."""

        if self.coalescer is not None:
            try:
                result: CoalescerResult = self.coalescer.tick()
            except Exception as exc:  # noqa: BLE001 - recorded in status; full backups still run
                detail = f"{JOURNAL_CHECK_PREFIX}{describe_error(exc)}"[:MAX_DETAIL_CHARS]
                if detail != self._coalescer_error:
                    if self._coalescer_error is None:
                        with self._state_lock:
                            prior = self._status["classes"].get("config")
                            self._coalescer_prior_run = None if _is_journal_check_record(prior) else prior
                    self._coalescer_error = detail
                    logger.error("%s", detail)
                    self._record_class("config", RunRecord(at=_iso(self._clock()) or "", ok=False, detail=detail))
            else:
                if self._coalescer_error is not None:
                    # Readable again: put back the last real run unless a backup replaced it.
                    with self._state_lock:
                        current = self._status["classes"].get("config")
                        restore = isinstance(current, dict) and current.get("detail") == self._coalescer_error
                        if restore and self._coalescer_prior_run is not None:
                            self._status["classes"]["config"] = self._coalescer_prior_run
                        elif restore:
                            self._status["classes"].pop("config", None)
                    self._coalescer_error = None
                    self._coalescer_prior_run = None
                    if restore:
                        self._write_status()
                if result.status == "failed":
                    logger.warning("Config backup failed: %s", result.error)
                elif result.status in {"backup", "noop"}:
                    logger.info("Config backup %s: %d change(s).", result.status, len(result.change_ids))
        now = self._clock()
        if self.next_full_at is not None and now >= self.next_full_at:
            self.next_full_at = self._compute_next_full(now)
            try:
                self.run_now("full")
            except SchedulerBusyError:
                logger.info("Scheduled full backup deferred: another backup run is in progress.")
                self.next_full_at = now + timedelta(minutes=1)
            except Exception as exc:  # noqa: BLE001 - recorded in status; loop continues
                logger.error("Scheduled full backup failed with %s.", type(exc).__name__)

    def seconds_until_next_tick(self, *, maximum: float = 5.0) -> float:
        waits = [maximum]
        if self.coalescer is not None:
            remaining = self.coalescer.seconds_until_due()
            if remaining is not None:
                waits.append(remaining)
        if self.next_full_at is not None:
            waits.append((self.next_full_at - self._clock()).total_seconds())
        return max(0.2, min(waits))

    # -- running backups ---------------------------------------------------------------

    @property
    def running(self) -> dict[str, str] | None:
        with self._state_lock:
            return dict(self._running) if self._running else None

    def _reserve(self, label: str) -> None:
        if not self._job_lock.acquire(blocking=False):
            raise SchedulerBusyError("A backup or grooming run is already in progress.")
        with self._state_lock:
            self._running = {"backup_class": label, "started_at": _iso(self._clock()) or ""}

    def _release(self) -> None:
        with self._state_lock:
            self._running = None
        self._job_lock.release()

    @contextlib.contextmanager
    def _job(self, label: str) -> Iterator[None]:
        if getattr(self._job_owner, "held", False):
            yield  # this thread already holds the reservation (start_run worker)
            return
        self._reserve(label)
        try:
            yield
        finally:
            self._release()

    def start_run(self, backup_class: str) -> threading.Thread:
        """Reserve the job lock now, then run ``backup_class`` in a worker thread.

        The reservation is taken before this returns, so a caller that answers
        "started" is never racing a scheduled tick for the lock.
        """

        if backup_class not in BACKUP_CLASSES:
            raise ValueError("backup_class must be config or full.")
        self._reserve(backup_class)

        def worker() -> None:
            self._job_owner.held = True
            try:
                self.run_now(backup_class)
            except Exception as exc:  # noqa: BLE001 - recorded in status by _run_class
                logger.error("Requested %s backup failed: %s", backup_class, type(exc).__name__)
            finally:
                self._job_owner.held = False
                self._release()

        thread = threading.Thread(target=worker, name=f"backup-run-{backup_class}", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._release()
            raise
        return thread

    def _run_class_locked_or_busy(
        self, backup_class: str, change_ids: tuple[str, ...],
        snapshot: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        # Called by the coalescer; a busy job lock is a failure it retries with backoff.
        with self._job(backup_class):
            return self._run_class(backup_class, change_ids, snapshot)

    def run_now(self, backup_class: str) -> ArtifactRecord | None:
        """Run a backup of ``backup_class`` now (single-flight)."""

        if backup_class not in BACKUP_CLASSES:
            raise ValueError("backup_class must be config or full.")
        if backup_class == "config" and self.coalescer is not None:
            # Through the coalescer so pending journal entries are committed to it.
            if not getattr(self._job_owner, "held", False):
                with self._job("config"):
                    pass  # fail fast with SchedulerBusyError when something is running
            result = self.coalescer.run()
            if result.status == "busy":
                raise SchedulerBusyError("A backup or grooming run is already in progress.")
            if result.status == "failed":
                raise RuntimeError(result.error or "config backup failed")
            if result.status in {"noop", "idle"}:
                return self._run_class_locked_or_busy("config", ())
            return result.record
        with self._job(backup_class):
            return self._run_class(backup_class, ())

    def _runner(
        self,
        backup_class: str,
        snapshot: Mapping[str, Any] | None = None,
        *,
        before_publish: Callable[[Path, Path], None],
    ) -> Any:
        status_file = (
            self.paths.full_status_file
            if backup_class == "full" and self.paths.full_status_file is not None
            else self.paths.runner_status_dir / f"archive-{backup_class}-backup.json"
        )
        backup_service = self.backup_service
        if isinstance(snapshot, ConfigFileSnapshot):
            backup_service = backup_service.with_captured_config_files(snapshot.files)
        return self._runner_factory(
            backup_service,
            destination_dir=self.paths.local_dir / backup_class,
            status_file=status_file,
            passphrase_file=self.paths.passphrase_file,
            included_groups=self.groups[backup_class],
            retention_count=1,
            app_gid=self.app_gid,
            clock=self._clock,
            apply_retention=False,
            before_publish=before_publish,
            archive_format=self.policy.full.archive_format if backup_class == "full" else "7z",
        )

    def _run_class(
        self, backup_class: str, change_ids: tuple[str, ...],
        snapshot: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        started = self._clock()
        artifact_id = new_artifact_id()
        try:
            runner = self._runner(
                backup_class,
                snapshot,
                before_publish=lambda temporary, target: self._remember_publication(
                    artifact_id, backup_class, started, change_ids, temporary, target
                ),
            )
            status = runner.run_once()
            name = f"{backup_class}/{status['last_artifact_name']}"
            record = ArtifactRecord(
                artifact_id=artifact_id,
                backup_class=backup_class,  # type: ignore[arg-type]
                location=LOCAL_LOCATION,
                name=name,
                created_at=started,
                size=int(status["last_size_bytes"]),
                sha256=str(status["last_sha256"]),
                verified=True,  # the runner preflighted and hashed the published file
                change_ids=tuple(change_ids),
            )
            self.catalog.add(record, now=started)
            can_replace_history = self._full_can_replace_history_snapshots(
                backup_class, status
            )
            manifest = getattr(runner, "last_manifest", None) or {}
            artifact_meta = _manifest_meta(manifest, self.groups[backup_class])
            artifact_meta["history_replacement"] = can_replace_history
            with self._state_lock:
                self._meta[record.artifact_id] = artifact_meta
                self._save_meta()
            if can_replace_history:
                with self._state_lock:
                    self._status["verified_full"] = {
                        "artifact_id": record.artifact_id,
                        "created_at": _iso(record.created_at),
                        "included_groups": list(self.groups[backup_class]),
                    }
        except Exception as exc:
            self._record_class(backup_class, RunRecord(at=_iso(started) or "", ok=False, detail=describe_error(exc)))
            raise
        ship_failures = self._ship(record)
        detail = None if not ship_failures else f"{len(ship_failures)} remote copy failure(s)"
        self._record_class(
            backup_class,
            RunRecord(at=_iso(started) or "", ok=True, detail=detail, artifact_id=record.artifact_id),
        )
        grooming_succeeded = False
        # Local FULL payload reads made by grooming's keeper check in this run
        # (None when the read did not complete). The sidecar credit below
        # reuses them and counts them against its own 14-read allowance.
        local_digests: _LocalReads = {}
        try:
            grooming = self._groom_locked(local_digests)
            grooming_succeeded = grooming is None or grooming.location_complete(LOCAL_LOCATION)
        except Exception as exc:  # noqa: BLE001 - grooming failure never fails the backup
            logger.warning("Backup grooming failed after %s backup (%s).", backup_class, type(exc).__name__)
        if can_replace_history and grooming_succeeded:
            full_copy_count = self._verified_local_full_count(local_digests)
            sidecar_keep = max(0, HISTORY_REPLACEMENT_COPIES - full_copy_count)
            try:
                removed = self._remove_replaced_history_snapshots(
                    record.created_at,
                    keep_count=sidecar_keep,
                )
            except Exception as exc:  # noqa: BLE001 - the verified FULL remains valid if cleanup fails
                logger.warning(
                    "Older history sidecar copies could not be removed after the verified FULL (%s).",
                    type(exc).__name__,
                )
            else:
                if removed:
                    logger.info(
                        "Removed %d replaced history sidecar backup %s; "
                        "%d digest-checked local FULL %s.",
                        removed,
                        "copy" if removed == 1 else "copies",
                        full_copy_count,
                        "copy remains" if full_copy_count == 1 else "copies remain",
                    )
        return record

    def _full_can_replace_history_snapshots(
        self,
        backup_class: str,
        runner_status: Mapping[str, Any],
    ) -> bool:
        included = runner_status.get("included_groups")
        absent = runner_status.get("last_absent_groups")
        return (
            backup_class == "full"
            and isinstance(included, list)
            and isinstance(absent, list)
            and HISTORY_GROUP in included
            and HISTORY_GROUP not in absent
            and set(included) == set(self.groups["full"])
        )

    def _history_snapshot_specs(self) -> tuple[tuple[Path, re.Pattern[str]], ...]:
        backup_dir = self.paths.history_backup_dir
        if backup_dir is None:
            return ()
        stem = re.escape(self.paths.history_database_stem)
        specs: list[tuple[Path, re.Pattern[str]]] = [
            (
                backup_dir,
                re.compile(rf"^{stem}-[0-9]{{8}}T[0-9]{{6}}Z\.sqlite3$"),
            )
        ]
        if self.paths.history_long_term_backup_dir is not None:
            long_term = self.paths.history_long_term_backup_dir
            specs.extend(
                (
                    (
                        long_term / "weekly",
                        re.compile(rf"^{stem}-weekly-[0-9]{{4}}-W[0-9]{{2}}\.sqlite3$"),
                    ),
                    (
                        long_term / "monthly",
                        re.compile(rf"^{stem}-monthly-[0-9]{{4}}-[0-9]{{2}}\.sqlite3$"),
                    ),
                )
            )
        return tuple(specs)

    @staticmethod
    def _snapshot_identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def _verified_local_full_count(self, digests: _LocalReads | None = None) -> int:
        """Local FULLs with history whose bytes match the catalog right now (#1188).

        Candidates are checked newest first with a bounded size-and-SHA-256
        read. ``digests`` carries the local FULL reads grooming's keeper check
        already made in this run (``None`` for a read that did not complete).
        Those count against ``HISTORY_REPLACEMENT_COPIES`` first and are
        reused, never repeated, so the keeper check and this check together
        read at most that many local FULLs in one run. Every attempt counts,
        whether or not it earns credit, so corrupt or unreadable copies cannot
        make this read every FULL. A copy that is missing or does not match is
        marked unverified and earns no credit; a copy that cannot be read
        earns no credit either. After a downgrade the status is published
        again so a ``verified_full`` receipt for that copy is withdrawn.
        """

        metadata = self._metadata_snapshot()
        local = LocalBackupDirectory(self.paths.local_dir)
        prior = dict(digests or {})
        unused = set(prior)
        count = 0
        attempts = len(prior)
        downgraded = False
        for artifact in reversed(self.catalog.list(backup_class="full", location=LOCAL_LOCATION)):
            reused = artifact.artifact_id in prior
            unused.discard(artifact.artifact_id)
            if not reused and attempts >= HISTORY_REPLACEMENT_COPIES:
                if not unused:
                    break
                continue
            meta = metadata.get(artifact.artifact_id) or {}
            if meta.get("history_replacement") is not True or self._artifact_state(artifact, meta) != "ok":
                continue
            if reused:
                observed = prior[artifact.artifact_id]
                if observed is None:
                    continue  # grooming could not read it; already counted
            else:
                attempts += 1
                try:
                    observed = local_payload_digest(artifact, local)
                except FileNotFoundError:
                    observed = (-1, "")
                except KeeperCheckUnavailable as exc:
                    logger.warning("A local FULL earned no history sidecar credit (%s).", exc.reason)
                    continue
            if observed != (artifact.size, artifact.sha256):
                self.catalog.mark_unverified(artifact.artifact_id)
                downgraded = True
                continue
            count += 1
        if downgraded:
            self._write_status()
        return count

    @staticmethod
    def _open_snapshot_directory(root: Path) -> int | None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            return os.open(root, flags)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                return None
            raise

    def _remove_replaced_history_snapshots(
        self,
        replacement_at: datetime,
        *,
        keep_count: int,
    ) -> int:
        """Remove only excess sidecar snapshots older than a verified FULL.

        Scheduler FULLs and retained sidecars jointly maintain the 14-copy
        replacement floor. The caller runs this after archive grooming, so only
        local FULLs that still exist and remain catalog-verified count toward
        that floor. Same-age/newer files, unrelated names, symlinks, and
        hard-linked files are left alone.
        """

        replacement_ns = int(replacement_at.astimezone(timezone.utc).timestamp() * 1_000_000_000)
        specs = self._history_snapshot_specs()
        candidates: list[tuple[int, int, str, tuple[int, ...]]] = []
        for spec_index, (root, name_pattern) in enumerate(specs):
            descriptor = self._open_snapshot_directory(root)
            if descriptor is None:
                continue
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        if name_pattern.fullmatch(entry.name) is None:
                            continue
                        try:
                            metadata = os.stat(
                                entry.name,
                                dir_fd=descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_nlink != 1
                            or metadata.st_mtime_ns >= replacement_ns
                        ):
                            continue
                        candidates.append(
                            (
                                metadata.st_mtime_ns,
                                spec_index,
                                entry.name,
                                self._snapshot_identity(metadata),
                            )
                        )
            finally:
                os.close(descriptor)

        remove_count = max(0, len(candidates) - max(0, keep_count))
        removed = 0
        for _, spec_index, name, initial_identity in sorted(candidates)[:remove_count]:
            root, name_pattern = specs[spec_index]
            if name_pattern.fullmatch(name) is None:
                continue
            descriptor = self._open_snapshot_directory(root)
            if descriptor is None:
                continue
            try:
                try:
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (
                    self._snapshot_identity(current) != initial_identity
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or current.st_mtime_ns >= replacement_ns
                ):
                    continue
                os.unlink(name, dir_fd=descriptor)
                os.fsync(descriptor)
                removed += 1
            finally:
                os.close(descriptor)
        return removed

    # -- shipping ----------------------------------------------------------------------

    @contextlib.contextmanager
    def _open_configured_target(self, target: ArchiveTarget) -> Iterator[Any]:
        # Recheck immediately before every operation. This catches a symlink or
        # bind-visible alias introduced after startup, before copy or retention
        # receives a second catalog identity for local bytes.
        peers = (target,)
        if target.settings.provider == "filesystem":
            validate_filesystem_target_roots((target,), self.paths.local_dir)
            peers = (
                target,
                *(peer for peer in self.policy.enabled_targets() if peer.target_id != target.target_id),
            )
            validate_filesystem_target_roots(peers, self.paths.local_dir, allow_unavailable=True)
        opened = (
            self._open_target(
                target.settings,
                local_archive_root=self.paths.local_dir,
                sftp_pin_file=self.paths.state_dir / SFTP_PIN_FILE_NAME,
            )
            if self._open_target_is_default
            else self._open_target(target.settings)
        )
        with opened as remote:
            if target.settings.provider == "filesystem" and isinstance(remote, LocalDirectoryTarget):
                def validate_target() -> None:
                    validate_filesystem_target_roots((target,), self.paths.local_dir)
                    validate_filesystem_target_roots(peers, self.paths.local_dir, allow_unavailable=True)

                remote.before_mutation = validate_target
            yield remote

    def _remote_record(
        self,
        source: ArtifactRecord,
        target: ArchiveTarget,
        stored: StoredObject,
        *,
        preserved: bool = False,
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=new_artifact_id(),
            backup_class=source.backup_class,
            location=target.target_id,
            name=source.name,
            created_at=source.created_at,
            size=stored.size,
            sha256=stored.sha256,
            verified=bool(stored.verified),
            change_ids=source.change_ids,
            preserved=preserved,
            preserve_reason=(
                "Remote publication has uncertain directory durability; verify before releasing preservation."
                if preserved
                else ""
            ),
            preserved_by="scheduler-recovery" if preserved else "",
        )

    def _catalog_remote(
        self,
        source: ArtifactRecord,
        remote_record: ArtifactRecord,
        *,
        upload_check: str = "readback",
        upload_provider: str | None = None,
    ) -> None:
        self.catalog.add(remote_record)
        with self._state_lock:
            # Copy only what describes the archive itself. Verify results and
            # publication intents belong to the source copy: a Verify of the local
            # file must never make this remote copy look read back (#1202).
            source_meta = self._metadata_snapshot(source.artifact_id)
            meta = {key: source_meta[key] for key in _ARCHIVE_DESCRIPTION_KEYS if key in source_meta}
            # #722: remember how the upload was checked, so the detail view can
            # say "size only" until an explicit verify re-reads and hashes it.
            meta["upload_check"] = upload_check
            # #1288: and which provider took the upload, so the check is read
            # from this copy's record and never from the target's current settings.
            if upload_provider is not None:
                meta["upload_provider"] = upload_provider
            self._meta[remote_record.artifact_id] = meta
            self._save_meta()

    def _ship(self, record: ArtifactRecord) -> list[str]:
        failures: list[str] = []
        local_path = self._local_path(record)
        for target in self.policy.enabled_targets():
            at = _iso(self._clock()) or ""
            try:
                with self._open_configured_target(target) as remote:
                    stored = remote.put(local_path, record.name)
                    if not stored.verified:
                        try:
                            remote.delete(record.name)
                        except Exception:  # noqa: BLE001 - cleanup must not mask the verify failure
                            logger.warning("Unverified remote backup copy on %s could not be removed.", target.target_id)
                        raise RuntimeError("remote copy could not be verified")
                if stored.size != record.size or stored.sha256 != record.sha256:
                    raise RuntimeError("remote copy does not match the local archive")
                remote_record = self._remote_record(record, target, stored)
                self._catalog_remote(
                    record, remote_record, upload_check=stored.upload_check, upload_provider=target.settings.provider
                )
                self._record_target(target.target_id, RunRecord(at=at, ok=True, artifact_id=remote_record.artifact_id))
            except ArchivePublicationUncertainError as exc:
                remote_record = self._remote_record(record, target, exc.stored, preserved=True)
                try:
                    if remote_record.size != record.size or remote_record.sha256 != record.sha256:
                        raise RuntimeError("uncertain remote copy does not match the local archive")
                    self._catalog_remote(
                        record,
                        remote_record,
                        upload_check=exc.stored.upload_check,
                        upload_provider=target.settings.provider,
                    )
                except Exception as catalog_exc:  # noqa: BLE001 - keep target failure isolated
                    logger.warning(
                        "Uncertain remote backup copy to %s could not be catalogued (%s).",
                        target.target_id,
                        type(catalog_exc).__name__,
                    )
                logger.warning(
                    "Remote backup copy to %s has uncertain directory durability.",
                    target.target_id,
                )
                self._record_target(
                    target.target_id,
                    RunRecord(
                        at=at,
                        ok=False,
                        detail=describe_error(exc),
                        artifact_id=remote_record.artifact_id,
                    ),
                )
                failures.append(target.target_id)
            except Exception as exc:  # noqa: BLE001 - one target failing must not stop the others
                logger.warning("Remote backup copy to %s failed (%s).", target.target_id, type(exc).__name__)
                self._record_target(target.target_id, RunRecord(at=at, ok=False, detail=describe_error(exc)))
                failures.append(target.target_id)
        self._write_status()
        return failures

    # -- grooming ----------------------------------------------------------------------

    def retention_rules(self) -> list[RetentionRule]:
        rules: list[RetentionRule] = []
        for backup_class in BACKUP_CLASSES:
            class_policy = self.policy.class_policy(backup_class)
            rules.append(RetentionRule(backup_class, LOCAL_LOCATION, keep_count=class_policy.local_keep))
            if class_policy.remote_keep is None and class_policy.remote_max_age_days is None:
                continue
            for target in self.policy.enabled_targets():
                rules.append(
                    RetentionRule(
                        backup_class,
                        target.target_id,
                        keep_count=class_policy.remote_keep,
                        max_age=self.policy.max_age(backup_class),
                    )
                )
        return rules

    def _manager(self, local_digests: _LocalReads | None = None) -> LifecycleManager:
        return LifecycleManager(
            self.catalog, self.retention_rules(), payload_digest=self._keeper_digest(local_digests)
        )

    def _keeper_digest(self, local_digests: _LocalReads | None = None) -> PayloadDigest:
        """How grooming re-reads a kept copy before it deletes anything (#1188).

        Local copies are hashed in place. A remote copy is read back through
        the target's streaming ``get`` into a private scratch file under the
        state directory with a hard limit of the recorded size, the same
        bounded readback an explicit verify uses; the target is never listed,
        so a large or hostile namespace adds no work. Nothing is buffered in
        memory and the scratch file is removed at once. The transports have no
        digest-only read, so the scratch file stays: leftover ``keeper-check-*``
        directories from an interrupted run are removed first, and the check
        is refused with ``keeper-scratch-capacity`` unless the state
        filesystem has room for the copy plus ``KEEPER_SCRATCH_HEADROOM_BYTES``.
        A readback that reports the object as absent (ENOENT, directly or as
        the cause) fails the keeper; a target without ``get`` or any other
        readback failure refuses the deletion with a named reason and leaves
        the keeper's flag alone.
        """

        def digest(record: ArtifactRecord, target: GroomableTarget) -> tuple[int, str]:
            if record.location == LOCAL_LOCATION:
                # Record local FULL attempts before reading, so a failed read
                # still counts against the run's sidecar-credit allowance.
                reads = local_digests if record.backup_class == "full" else None
                if reads is not None:
                    reads[record.artifact_id] = None
                observed = local_payload_digest(record, target)
                if reads is not None:
                    reads[record.artifact_id] = observed
                return observed
            get: Any = getattr(target, "get", None)
            if not callable(get):
                raise KeeperCheckUnavailable("keeper-unverifiable")
            try:
                _remove_stale_keeper_scratch(self.paths.state_dir)
                free = _scratch_free_bytes(self.paths.state_dir)
            except OSError as exc:
                raise KeeperCheckUnavailable("keeper-readback-failed") from exc
            if free < record.size + KEEPER_SCRATCH_HEADROOM_BYTES:
                raise KeeperCheckUnavailable("keeper-scratch-capacity")
            try:
                workspace = Path(tempfile.mkdtemp(prefix=KEEPER_SCRATCH_PREFIX, dir=self.paths.state_dir))
            except OSError as exc:
                # Never read as "keeper missing": that would downgrade a good copy.
                raise KeeperCheckUnavailable("keeper-readback-failed") from exc
            try:
                size, sha256 = get(record.name, workspace / "archive", limit=record.size)
                return int(size), str(sha256)
            except ArchiveDownloadTooLargeError:
                return record.size + 1, ""
            except (ConnectionError, TimeoutError):
                raise
            except Exception as exc:  # noqa: BLE001 - refuse, never delete on an unchecked keeper
                if _reports_absent(exc):
                    raise FileNotFoundError(record.name) from exc
                raise KeeperCheckUnavailable("keeper-readback-failed") from exc
            finally:
                for child in workspace.iterdir():
                    child.unlink(missing_ok=True)
                workspace.rmdir()

        return digest

    @contextlib.contextmanager
    def _resolver(self) -> Iterator[Callable[[str], Any]]:
        with contextlib.ExitStack() as stack:
            opened: dict[str, Any] = {}

            def remote(location: str) -> Any:
                if location not in opened:
                    target = self.policy.target(location)
                    if target is None:
                        raise LookupError(f"No archive target is configured for location {location!r}.")
                    opened[location] = stack.enter_context(self._open_configured_target(target))
                return opened[location]

            yield local_target_resolver(self.paths.local_dir, remote)

    def _groom_locked(self, local_digests: _LocalReads | None = None) -> Any:
        manager = self._manager(local_digests)
        plan = manager.plan(self._clock())
        if not plan.items:
            self._record_grooming(ok=True, deleted=0, detail=None, failed_locations={})
            return None
        with self._resolver() as resolver:
            result = manager.apply(plan, resolver, actor="scheduler", now=self._clock)
        if result.error:
            logger.warning("Backup grooming stopped early (%s).", result.error.split(":", 1)[0])
        self._record_grooming_result(result)
        return result

    def _record_grooming_result(self, result: Any) -> None:
        failed_locations = dict(result.failed_locations)
        if result.failed is not None and result.error and result.failed.record.location not in failed_locations:
            # A location that opened but then failed a deletion or claim stopped the run.
            failed_locations[result.failed.record.location] = result.error
        detail = result.error
        downgraded = getattr(result, "downgraded", ())
        if downgraded:
            # #1188: a kept copy failed its payload check; say so even when the
            # replanned run otherwise completed.
            count = len(downgraded)
            # "N of the copies it kept", so the count cannot read as a tally of kept copies.
            note = (
                f"{count} of the copies it kept failed the size and SHA-256 "
                f"check and {'was' if count == 1 else 'were'} marked unverified."
            )
            detail = note if detail is None else f"{detail} {note}"
            logger.warning(
                "Backup grooming marked %d kept %s unverified.", count, "copy" if count == 1 else "copies"
            )
        self._record_grooming(
            ok=not result.error,
            deleted=len(result.deleted) + len(result.already_missing),
            detail=detail,
            failed_locations=failed_locations,
        )

    def _record_grooming(
        self, *, ok: bool, deleted: int, detail: str | None, failed_locations: Mapping[str, str]
    ) -> None:
        def short(text: str | None) -> str | None:
            return None if text is None else " ".join(text.split())[:MAX_DETAIL_CHARS]

        with self._state_lock:
            self._status["grooming"] = {
                "at": _iso(self._clock()),
                "ok": ok,
                "deleted": deleted,
                "detail": short(detail),
                "failed_locations": {location: short(text) for location, text in failed_locations.items()},
            }
        self._write_status()

    def plan(self) -> tuple[str, datetime, GroomingPlan]:
        plan = self._manager().plan(self._clock())
        now = self._monotonic()
        with self._state_lock:
            for token, entry in list(self._plans.items()):
                if entry.expires_at < now:
                    del self._plans[token]
            while len(self._plans) >= MAX_PLANS:
                del self._plans[next(iter(self._plans))]
            token = secrets.token_urlsafe(24)
            self._plans[token] = _PlanEntry(expires_at=now + PLAN_TTL_SECONDS, plan=plan)
        return token, self._clock() + timedelta(seconds=PLAN_TTL_SECONDS), plan

    def apply(self, plan_token: Any) -> Any:
        with self._state_lock:
            entry = self._plans.pop(plan_token, None) if isinstance(plan_token, str) else None
        if entry is None or entry.expires_at < self._monotonic():
            raise LookupError("The grooming plan expired or was already used; preview it again.")
        with self._job("lifecycle"), self._resolver() as resolver:
            result = self._manager().apply(entry.plan, resolver, actor="admin", now=self._clock)
            self._record_grooming_result(result)
        return result

    # -- library queries ---------------------------------------------------------------

    def _local_path(self, record: ArtifactRecord) -> Path:
        path = self.paths.local_dir.joinpath(*record.name.split("/"))
        parent = path.parent.resolve()
        root = self.paths.local_dir.resolve()
        if parent != root and root not in parent.parents:
            raise ValueError("Artifact path escapes the backup directory.")
        return path

    def get(self, artifact_id: str) -> ArtifactRecord:
        record = self.catalog.get(artifact_id) if isinstance(artifact_id, str) else None
        if record is None:
            raise ArtifactNotFoundError("Backup not found.")
        return record

    def artifact_state(self, record: ArtifactRecord) -> str:
        return self._artifact_state(record, self._metadata_snapshot(record.artifact_id))

    def _artifact_state(self, record: ArtifactRecord, meta: Mapping[str, Any]) -> str:
        schema = meta.get("schema_version")
        if schema is not None and schema not in (1, 2):
            return "unsupported"
        if record.location == LOCAL_LOCATION:
            try:
                metadata = os.lstat(self._local_path(record))
            except (OSError, ValueError):
                return "missing"
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != record.size:
                return "incomplete"
        if not record.verified:
            return "incomplete"
        return "ok"

    def serialize(self, record: ArtifactRecord) -> dict[str, Any]:
        return self._serialize(record, self._metadata_snapshot(record.artifact_id))

    def _serialize(self, record: ArtifactRecord, meta: Mapping[str, Any]) -> dict[str, Any]:
        state = self._artifact_state(record, meta)
        return {
            "id": record.artifact_id,
            "backup_class": record.backup_class,
            "location": record.location,
            "created_at": _iso(record.created_at),
            "size": record.size,
            "sha256": record.sha256,
            "verified": record.verified,
            "restorable": state == "ok",
            "state": state,
            "preserved": record.preserved,
            "preserve_reason": record.preserve_reason or None,
            "preserved_by": record.preserved_by or None,
            "change_count": len(record.change_ids),
            "app_version": meta.get("app_version"),
            # #1202: the list says what the details say. None for local copies
            # and copies catalogued before #722.
            "upload_check": self._presented_upload_check(record, meta),
            "last_verify": _list_last_verify(record, meta),
        }

    def _presented_upload_check(self, record: ArtifactRecord, meta: Mapping[str, Any]) -> str | None:
        """How this copy was checked at upload, claiming no more than the record shows.

        Earlier releases stored "readback" for an S3 upload whose ETag matched,
        which read nothing back, and the stored record cannot tell that apart
        from a real readback (#1202). Such a record is presented as either until
        its own Verify passes. The provider is the one this copy recorded at
        upload, never the target's current one: a target's provider can change
        after the copy was made, so a record that names no known provider is
        presented as either too (#1288).
        """

        value = _upload_check(meta)
        if value == "readback" and record.location != LOCAL_LOCATION:
            provider = meta.get("upload_provider")
            if not (isinstance(provider, str) and provider in PROVIDERS) or provider == "s3":
                return "provider_checksum_or_readback"
        return value

    def detail(self, artifact_id: str) -> dict[str, Any]:
        record = self.get(artifact_id)
        meta = self._metadata_snapshot(record.artifact_id)
        payload = self._serialize(record, meta)
        entries: dict[str, Any] = {}
        if record.change_ids and self.paths.journal_path.exists():
            try:
                journal = self.journal or ChangeJournal(self.paths.journal_path, file_mode=0o660)
                entries = {entry.change_id: entry for entry in journal.entries()}
            except Exception as exc:  # noqa: BLE001 - detail stays useful without the journal
                logger.warning("Change journal could not be read (%s).", type(exc).__name__)
        payload["changes"] = [
            {
                "change_id": change_id,
                "at": _iso(entries[change_id].at) if change_id in entries else None,
                "action": entries[change_id].action if change_id in entries else None,
                "subject": entries[change_id].subject if change_id in entries else None,
            }
            for change_id in record.change_ids
        ]
        payload["inspect"] = (
            {
                "groups": meta.get("groups") or [],
                "encrypted": True,
                "schema_version": meta.get("schema_version"),
                "app_version": meta.get("app_version"),
                "packaging": meta.get("packaging"),
            }
            if meta
            else None
        )
        payload["last_verify"] = _detail_last_verify(record, meta)
        return payload

    def library(self) -> dict[str, Any]:
        # Prevent intent clearing between the catalog read and metadata copy.
        # Lock order is state -> catalog; catalog methods never acquire state.
        # Validation and filesystem inspection below use only the detached copy.
        with self._state_lock:
            records = self.catalog.list()
            metadata = self._metadata_snapshot()
            deleted = (
                {item.record.artifact_id for item in self.catalog.tombstones(location=LOCAL_LOCATION)}
                if any(meta.get("publication") is not None for meta in metadata.values()) else set()
            )
        known = {record.artifact_id for record in records}
        pending_publications = self._pending_publications(metadata)
        if pending_publications:
            known.update(deleted)
            pending_publications = [record for record in pending_publications if record.artifact_id not in known]
        records.extend(pending_publications)
        storage: dict[str, dict[str, int]] = {}
        for record in records:
            bucket = storage.setdefault(record.location, {"config_bytes": 0, "full_bytes": 0, "count": 0})
            bucket[f"{record.backup_class}_bytes"] += record.size
            bucket["count"] += 1
        with self._state_lock:
            class_runs = dict(self._status["classes"])
            target_runs = dict(self._status["targets"])
            grooming = self._status.get("grooming")
            # #1188: read the job reservation with the stored clean-up result,
            # so one answer never pairs an earlier result with a later idle state.
            running = self.running
        pending = 0
        if self.coalescer is not None:
            try:
                pending = self.coalescer.poll()
            except Exception:  # noqa: BLE001 - informational
                pending = 0
        config = self.policy.config
        full = self.policy.full
        return {
            "available": True,
            "detail": (
                "Interrupted backup publications require recovery; pending sizes are recorded intent sizes."
                if pending_publications else None
            ),
            "running": running,
            "classes": {
                "config": {
                    "enabled": config.enabled,
                    "debounce_seconds": config.debounce_seconds,
                    "max_delay_seconds": config.max_delay_seconds,
                    "local_keep": config.local_keep,
                    "remote_keep": config.remote_keep,
                    "remote_max_age_days": config.remote_max_age_days,
                    "pending_changes": pending,
                    "last_run": class_runs.get("config"),
                },
                "full": {
                    "enabled": full.enabled,
                    "schedule": full.schedule,
                    "next_run_at": _iso(self.next_full_at),
                    "local_keep": full.local_keep,
                    "remote_keep": full.remote_keep,
                    "remote_max_age_days": full.remote_max_age_days,
                    "last_run": class_runs.get("full"),
                },
            },
            "targets": [
                {
                    "id": target.target_id,
                    "label": target.label,
                    "provider": target.settings.provider,
                    "transport_encrypted": transport_encrypted(target.settings),
                    "enabled": target.enabled,
                    "last_run": _target_run(target_runs.get(target.target_id)),
                }
                for target in self.policy.targets
            ],
            "artifacts": [
                self._serialize(record, metadata.get(record.artifact_id) or {})
                for record in reversed(records)
            ],
            "storage": storage,
            "grooming": grooming,
        }

    # -- per-artifact actions ----------------------------------------------------------

    def preserve(self, artifact_id: str, *, reason: str, actor: str) -> dict[str, Any]:
        self.get(artifact_id)
        record = self.catalog.set_preserve(artifact_id, reason=reason, actor=actor, now=self._clock())
        return self.serialize(record)

    def unpreserve(self, artifact_id: str, *, actor: str) -> dict[str, Any]:
        self.get(artifact_id)
        record = self.catalog.clear_preserve(artifact_id, actor=actor, now=self._clock())
        return self.serialize(record)

    @contextlib.contextmanager
    def materialize(self, artifact_id: str) -> Iterator[tuple[ArtifactRecord, Path]]:
        """Yield a readable local file with the artifact's bytes (remote copies are fetched)."""

        record = self.get(artifact_id)
        if record.location == LOCAL_LOCATION:
            path = self._local_path(record)
            if not path.is_file() or path.is_symlink():
                raise ArtifactNotFoundError("The local copy is missing.")
            _require_match(record, *_hash_file(path))
            yield record, path
            return
        target = self.policy.target(record.location)
        if target is None:
            raise ArtifactNotFoundError("The target holding this copy is no longer configured.")
        workspace = Path(tempfile.mkdtemp(prefix="backup-fetch-", dir=self.paths.state_dir))
        try:
            local = workspace / "archive"
            with self._open_configured_target(target) as remote:
                size, digest = remote.get(record.name, local, limit=record.size + 1)
            # Never hand out bytes the catalogue did not record: a target (or an
            # on-path attacker for plain FTP/NFS) could substitute another backup.
            _require_match(record, size, digest)
            yield record, local
        finally:
            try:
                for child in workspace.iterdir():
                    child.unlink(missing_ok=True)
                workspace.rmdir()
            except OSError as exc:
                # Never let clean-up replace the result of the read and comparison (#1202).
                logger.error("Backup fetch workspace could not be removed (%s).", type(exc).__name__)

    def verify(self, artifact_id: str) -> dict[str, Any]:
        at = _iso(self._clock())
        ok = False
        # Which of three things happened (#1202), decided by where the attempt
        # stopped and never by an error's text: "matched", "mismatch" (the
        # comparison ran and failed, or the local copy is gone), or "not_read"
        # (opening or reading the copy failed, so nothing was checked).
        outcome: str | None = "not_read"
        record = self.get(artifact_id)
        ambient = sys.exc_info()[1]  # what the caller is already handling is not about this copy
        try:
            with self.materialize(artifact_id):
                outcome = None  # read and matched; a later failure claims no outcome
            self.catalog.mark_verified(artifact_id, sha256=record.sha256, size=record.size, now=self._clock())
            ok = True
            outcome = "matched"
            detail = "Readback matched the catalogued size and SHA-256."
        except (ArchiveIntegrityError, ArchiveDownloadTooLargeError) as exc:
            # A copy that no longer matches must lose its verified flag, so it is
            # neither offered for restore nor protected as the newest copy that
            # passed its last completed check.
            self.catalog.mark_unverified(artifact_id)
            outcome = "mismatch"
            detail = str(exc)
        except ArtifactNotFoundError as exc:
            if record.location != LOCAL_LOCATION:
                raise
            self.catalog.mark_unverified(artifact_id)
            outcome = "mismatch"
            detail = str(exc)
        except Exception as exc:  # noqa: BLE001 - e.g. target unreachable; state unknown, keep flag
            detail = describe_error(exc)
            rejection = rejected_copy_error(exc, ambient)
            if rejection is not None:  # a later failure (e.g. clean-up) must not hide the rejection
                self.catalog.mark_unverified(artifact_id)
                outcome, detail = "mismatch", str(rejection)
        with self._state_lock:
            meta = self._meta.setdefault(artifact_id, {})
            # The result names the copy it read, so it never counts for another (#1202).
            meta["last_verify"] = {"at": at, "ok": ok, "detail": detail, "artifact_id": artifact_id}
            if outcome is not None:
                meta["last_verify"]["outcome"] = outcome
            self._save_meta()
        # Re-publish (or clear) verified_full from the catalog's current flag so
        # the history sidecar never suppresses its own copy after a failed
        # readback of the replacement artifact.
        self._write_status()
        return {"ok": ok, "outcome": outcome, "artifact": self.serialize(self.get(artifact_id)), "detail": detail}

    def test_target(self, target_id: str) -> dict[str, Any]:
        target = self.policy.target(target_id)
        if target is None:
            raise ArtifactNotFoundError("Target not found.")
        try:
            with self._open_configured_target(target) as remote:
                return remote.test()
        except Exception as exc:  # noqa: BLE001 - connection failures are results, not errors
            return {
                "ok": False,
                "detail": describe_error(exc),
                "duration_ms": 0,
                "provider": target.settings.provider,
                "transport_encrypted": transport_encrypted(target.settings),
            }

    def close(self) -> None:
        self.catalog.close()


def _target_run(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"at": value.get("at"), "ok": bool(value.get("ok")), "detail": value.get("detail")}


def _upload_check(meta: Mapping[str, Any]) -> str | None:
    value = meta.get("upload_check")
    # A malformed stored value is unknown for that copy; it must not break the list.
    return value if isinstance(value, str) and value in {"readback", "provider_checksum", "size"} else None


def _own_last_verify(record: ArtifactRecord, meta: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The last explicit Verify result of this copy, or None.

    A result counts only when it names this copy. Earlier releases wrote no
    name and copied the local copy's result into each remote copy's metadata,
    so an unnamed result on a remote copy says nothing about that copy's bytes:
    the copy reads as its upload check until its own Verify reads it back. A
    local copy's metadata is written only by its own run and its own Verify,
    so its unnamed result is its own (#1202).
    """

    value = meta.get("last_verify")
    if not isinstance(value, Mapping):
        return None
    if "artifact_id" in value:
        return value if value["artifact_id"] == record.artifact_id else None
    return value if record.location == LOCAL_LOCATION else None


def _list_last_verify(record: ArtifactRecord, meta: Mapping[str, Any]) -> dict[str, Any] | None:
    """When this copy's last explicit Verify ran and whether it passed, without its text.

    Only a boolean ``True`` counts as passed, so a malformed record never
    upgrades a size-only copy. The details add the result's text.
    """

    value = _own_last_verify(record, meta)
    if value is None:
        return None
    at = value.get("at")
    return {"at": at if isinstance(at, str) else None, "ok": _verify_result(value)[0]}


def _detail_last_verify(record: ArtifactRecord, meta: Mapping[str, Any]) -> dict[str, Any] | None:
    """The list summary of this copy's last Verify plus its text, by the same rules."""

    value = _own_last_verify(record, meta)
    if value is None:
        return None
    detail = value.get("detail")
    summary = _list_last_verify(record, meta)
    return {**summary, "outcome": _verify_result(value)[1], "detail": detail if isinstance(detail, str) else None}


def _verify_result(value: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Whether a saved Verify result passed, and its outcome, for the list and the details alike.

    Only a boolean ``True`` counts as passed, so a malformed record never
    upgrades a size-only copy. Results from earlier releases carry no outcome
    and keep their boolean. A result that carries an outcome passes only with
    "matched": ``ok: true`` beside "not_read", "mismatch" or a malformed
    outcome contradicts itself, so it has not passed and claims no outcome
    (#1289). An outcome is shown only when it agrees with ``ok``.
    """

    ok = value.get("ok") is True
    if "outcome" not in value:
        return ok, None
    outcome = value["outcome"]
    if not (isinstance(outcome, str) and outcome in _VERIFY_OUTCOMES and (outcome == "matched") is ok):
        return False, None
    return ok, outcome


_VERIFY_OUTCOMES = frozenset({"matched", "mismatch", "not_read"})


# Metadata that describes the archive content and so holds for every copy of it.
_ARCHIVE_DESCRIPTION_KEYS = ("app_version", "schema_version", "packaging", "groups", "history_replacement")


def _manifest_meta(manifest: Mapping[str, Any], groups: list[str]) -> dict[str, Any]:
    return {
        "app_version": manifest.get("app_version") if isinstance(manifest.get("app_version"), str) else None,
        "schema_version": manifest.get("schema_version") if isinstance(manifest.get("schema_version"), int) else None,
        "packaging": manifest.get("packaging") if isinstance(manifest.get("packaging"), str) else None,
        "groups": list(groups),
    }


def _remove_stale_keeper_scratch(state_dir: Path) -> None:
    """Remove ``keeper-check-*`` directories this service left behind (#1188).

    Keeper checks run one at a time under the job reservation and remove their
    own scratch directory, so any directory found here was left by a killed or
    interrupted run. Only plain directories owned by this process's user are
    removed; symlinks and other owners are left alone.
    """

    owner = os.geteuid() if hasattr(os, "geteuid") else None
    for entry in Path(state_dir).iterdir():
        if not entry.name.startswith(KEEPER_SCRATCH_PREFIX):
            continue
        try:
            metadata = os.lstat(entry)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(metadata.st_mode) or (owner is not None and metadata.st_uid != owner):
            continue
        shutil.rmtree(entry)


def _scratch_free_bytes(path: Path) -> int:
    """Bytes available to unprivileged writers on the filesystem holding ``path``."""

    return shutil.disk_usage(path).free


def _reports_absent(exc: BaseException) -> bool:
    """True when a readback failed because the object does not exist (ENOENT)."""

    for candidate in (exc, exc.__cause__):
        if isinstance(candidate, FileNotFoundError) or (
            isinstance(candidate, OSError) and candidate.errno == errno.ENOENT
        ):
            return True
    return False


def _require_match(record: ArtifactRecord, size: int, digest: str) -> None:
    if size != record.size or digest != record.sha256:
        raise ArchiveIntegrityError("Readback does not match the catalogued size and SHA-256.")


def rejected_copy_error(exc: BaseException, ambient: BaseException | None = None) -> BaseException | None:
    """The size or content check's rejection behind ``exc``, by type, or None (#1202).

    ``ambient`` is the exception the caller was handling when the attempt began.
    That one object is skipped, and so is anything reached only through it, so a
    rejection the caller was already handling is not taken for this copy. Nothing
    else ties an exception to an attempt: an exception object kept from earlier
    and chained in later is examined like any other.
    """

    pending, seen = [exc], set()
    while pending and len(seen) < 32:  # bounded; ``seen`` also stops a cycle
        current = pending.pop()
        if current is ambient:
            continue
        if isinstance(current, (ArchiveIntegrityError, ArchiveDownloadTooLargeError)):
            return current
        if id(current) not in seen:
            seen.add(id(current))
            pending += [link for link in (current.__cause__, current.__context__) if link is not None]
    return None


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


# -- config snapshot for the coalescer's content hash -------------------------------------


class ConfigFileSnapshot(dict[str, Any]):
    """Hash projection plus the exact source bytes used to build that projection."""

    def __init__(self, documents: Mapping[str, Any], files: Mapping[Path, bytes | None]) -> None:
        super().__init__(documents)
        self.files = MappingProxyType(dict(files))


def snapshot_config_files(paths: Mapping[str, Path]) -> ConfigFileSnapshot:
    """Capture once for both hashing and export; missing documents remain hashed."""

    snapshot: dict[str, Any] = {}
    files: dict[Path, bytes | None] = {}
    for logical, path in paths.items():
        content = path.read_bytes() if path.exists() else None
        files[path.absolute()] = content
        if content is None:
            snapshot[logical] = None
            continue
        text = content.decode("utf-8")
        snapshot[logical] = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    return ConfigFileSnapshot(snapshot, files)


# -- shared status file ---------------------------------------------------------------------


def write_shared_status(path: Path, payload: Mapping[str, Any], *, app_gid: int) -> None:
    """Atomically write ``payload`` as 0640 JSON in a setgid 2750 directory owned by us."""

    directory = path.parent
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != SHARED_STATUS_DIRECTORY_MODE
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != app_gid
    ):
        raise ValueError("Backup status directory must be owned by the backup user and APP_GID with mode 2750.")
    content = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, STATUS_FILE_MODE)
    try:
        os.fchmod(descriptor, STATUS_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_archive_status(path: str | Path, *, max_bytes: int = 256 * 1024) -> dict[str, Any] | None:
    """Read the scheduler status file; ``None`` when absent, unsafe or malformed."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(Path(path), flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022 or metadata.st_size > max_bytes:
            return None
        content = os.read(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != STATUS_SCHEMA_VERSION:
        return None
    return payload


__all__ = [
    "ArchiveIntegrityError",
    "ArtifactNotFoundError",
    "BackupScheduler",
    "SchedulerBusyError",
    "SchedulerPaths",
    "config_group_keys",
    "read_archive_status",
    "snapshot_config_files",
    "write_shared_status",
]
