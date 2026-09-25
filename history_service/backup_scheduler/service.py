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
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import local as _ThreadLocal
from typing import Any

import yaml

from history_service.backup_archive.catalog import (
    LOCAL_LOCATION,
    ArtifactCatalog,
    ArtifactRecord,
    new_artifact_id,
)
from history_service.backup_archive.cron import CronSchedule
from history_service.backup_archive.journal import ChangeJournal, ConfigBackupCoalescer, CoalescerResult
from history_service.backup_archive.lifecycle import (
    GroomingPlan,
    LifecycleManager,
    RetentionRule,
    local_target_resolver,
)
from history_service.backup_archive.policy import BackupPolicy
from history_service.backup_archive.transport import open_target, transport_encrypted
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
_COPY_CHUNK = 1024 * 1024


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
    """Short operator-facing text. Transport errors never contain secret values."""

    message = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text[:MAX_DETAIL_CHARS]


def config_group_keys(all_backup_groups: tuple[str, ...], history_group: str) -> list[str]:
    """Config class = every default backup group except the history database."""

    return [key for key in all_backup_groups if key != history_group]


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
        open_target_fn: Callable[[Any], Any] = open_target,
        runner_factory: Callable[..., Any] = ScheduledBackupRunner,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        local_tz: Any = None,
    ) -> None:
        self.policy = policy
        self.backup_service = backup_service
        self.paths = paths
        self.app_gid = int(app_gid)
        self.groups = {"config": list(config_groups), "full": list(full_groups)}
        self._open_target = open_target_fn
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
        self._meta: dict[str, dict[str, Any]] = self._load_meta()
        self._load_status()
        self.journal: ChangeJournal | None = None
        self.coalescer: ConfigBackupCoalescer | None = None
        if policy.config.enabled:
            self.journal = ChangeJournal(paths.journal_path, file_mode=0o660)
            self.coalescer = ConfigBackupCoalescer(
                self.journal,
                snapshot_config=snapshot_config,
                make_backup=lambda change_ids: self._run_class_locked_or_busy("config", change_ids),
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
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_meta(self) -> None:
        temporary = self._meta_path.with_name(f".{self._meta_path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._meta, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._meta_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_status(self) -> None:
        if self.paths.status_file is None:
            return
        payload = read_archive_status(self.paths.status_file)
        if payload:
            self._status["classes"] = dict(payload.get("classes") or {})
            self._status["targets"] = dict(payload.get("targets") or {})
            verified_full = self._validated_verified_full_receipt(payload.get("verified_full"))
            if verified_full is not None:
                self._status["verified_full"] = verified_full

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
        if (
            record is None
            or record.backup_class != "full"
            or record.location != LOCAL_LOCATION
            or not record.verified
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
        return self._cron.next_after(local_now).astimezone(timezone.utc)

    def tick(self) -> None:
        """One scheduler step: config coalescer, then a due full backup."""

        if self.coalescer is not None:
            result: CoalescerResult = self.coalescer.tick()
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

    def _run_class_locked_or_busy(self, backup_class: str, change_ids: tuple[str, ...]) -> ArtifactRecord:
        # Called by the coalescer; a busy job lock is a failure it retries with backoff.
        with self._job(backup_class):
            return self._run_class(backup_class, change_ids)

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

    def _runner(self, backup_class: str) -> Any:
        status_file = (
            self.paths.full_status_file
            if backup_class == "full" and self.paths.full_status_file is not None
            else self.paths.runner_status_dir / f"archive-{backup_class}-backup.json"
        )
        return self._runner_factory(
            self.backup_service,
            destination_dir=self.paths.local_dir / backup_class,
            status_file=status_file,
            passphrase_file=self.paths.passphrase_file,
            included_groups=self.groups[backup_class],
            retention_count=1,
            app_gid=self.app_gid,
            clock=self._clock,
            apply_retention=False,
            archive_format=self.policy.full.archive_format if backup_class == "full" else "7z",
        )

    def _run_class(self, backup_class: str, change_ids: tuple[str, ...]) -> ArtifactRecord:
        started = self._clock()
        try:
            runner = self._runner(backup_class)
            status = runner.run_once()
            name = f"{backup_class}/{status['last_artifact_name']}"
            record = ArtifactRecord(
                artifact_id=new_artifact_id(),
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
            manifest = getattr(runner, "last_manifest", None) or {}
            self._meta[record.artifact_id] = _manifest_meta(manifest, self.groups[backup_class])
            self._save_meta()
            if self._full_can_replace_history_snapshots(backup_class, status):
                with self._state_lock:
                    self._status["verified_full"] = {
                        "artifact_id": record.artifact_id,
                        "created_at": _iso(record.created_at),
                        "included_groups": list(self.groups[backup_class]),
                    }
                try:
                    removed = self._remove_replaced_history_snapshots(record.created_at)
                except Exception as exc:  # noqa: BLE001 - the verified FULL remains valid if cleanup fails
                    logger.warning(
                        "Older history sidecar copies could not be removed after the verified FULL (%s).",
                        type(exc).__name__,
                    )
                else:
                    if removed:
                        logger.info(
                            "Removed %d older history sidecar backup copy/copies after the verified FULL.",
                            removed,
                        )
        except Exception as exc:
            self._record_class(backup_class, RunRecord(at=_iso(started) or "", ok=False, detail=describe_error(exc)))
            raise
        ship_failures = self._ship(record)
        detail = None if not ship_failures else f"{len(ship_failures)} remote copy failure(s)"
        self._record_class(
            backup_class,
            RunRecord(at=_iso(started) or "", ok=True, detail=detail, artifact_id=record.artifact_id),
        )
        try:
            self._groom_locked()
        except Exception as exc:  # noqa: BLE001 - grooming failure never fails the backup
            logger.warning("Backup grooming failed after %s backup (%s).", backup_class, type(exc).__name__)
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

    def _remove_replaced_history_snapshots(self, replacement_at: datetime) -> int:
        """Remove exact sidecar snapshots older than a verified scheduler FULL.

        The caller invokes this only after the replacement archive is published,
        preflighted, hashed and committed to the verified artifact catalog. A
        same-age/newer file, unrelated name, symlink, or hard-linked file is
        left alone. Scheduler artifacts (including pinned ones) live under a
        different root and are never candidates here.
        """

        replacement_ns = int(replacement_at.astimezone(timezone.utc).timestamp() * 1_000_000_000)
        removed = 0
        for root, name_pattern in self._history_snapshot_specs():
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(root, flags)
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                    continue
                raise
            try:
                root_removed = False
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        if name_pattern.fullmatch(entry.name) is None:
                            continue
                        try:
                            initial = os.stat(
                                entry.name,
                                dir_fd=descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        if (
                            not stat.S_ISREG(initial.st_mode)
                            or initial.st_nlink != 1
                            or initial.st_mtime_ns >= replacement_ns
                        ):
                            continue
                        try:
                            final = os.stat(
                                entry.name,
                                dir_fd=descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        if self._snapshot_identity(final) != self._snapshot_identity(initial):
                            continue
                        os.unlink(entry.name, dir_fd=descriptor)
                        removed += 1
                        root_removed = True
                if root_removed:
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return removed

    # -- shipping ----------------------------------------------------------------------

    def _ship(self, record: ArtifactRecord) -> list[str]:
        failures: list[str] = []
        local_path = self._local_path(record)
        for target in self.policy.enabled_targets():
            at = _iso(self._clock()) or ""
            try:
                with self._open_target(target.settings) as remote:
                    stored = remote.put(local_path, record.name)
                if stored.size != record.size or stored.sha256 != record.sha256:
                    raise RuntimeError("remote copy does not match the local archive")
                remote_record = ArtifactRecord(
                    artifact_id=new_artifact_id(),
                    backup_class=record.backup_class,
                    location=target.target_id,
                    name=record.name,
                    created_at=record.created_at,
                    size=stored.size,
                    sha256=stored.sha256,
                    verified=bool(stored.verified),
                    change_ids=record.change_ids,
                )
                self.catalog.add(remote_record)
                self._meta[remote_record.artifact_id] = dict(self._meta.get(record.artifact_id) or {})
                self._save_meta()
                self._record_target(target.target_id, RunRecord(at=at, ok=True, artifact_id=remote_record.artifact_id))
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
            for target in self.policy.targets:
                rules.append(
                    RetentionRule(
                        backup_class,
                        target.target_id,
                        keep_count=class_policy.remote_keep,
                        max_age=self.policy.max_age(backup_class),
                    )
                )
        return rules

    def _manager(self) -> LifecycleManager:
        return LifecycleManager(self.catalog, self.retention_rules())

    @contextlib.contextmanager
    def _resolver(self) -> Iterator[Callable[[str], Any]]:
        with contextlib.ExitStack() as stack:
            opened: dict[str, Any] = {}

            def remote(location: str) -> Any:
                if location not in opened:
                    target = self.policy.target(location)
                    if target is None:
                        raise LookupError(f"No archive target is configured for location {location!r}.")
                    opened[location] = stack.enter_context(self._open_target(target.settings))
                return opened[location]

            yield local_target_resolver(self.paths.local_dir, remote)

    def _groom_locked(self) -> Any:
        manager = self._manager()
        plan = manager.plan(self._clock())
        if not plan.items:
            return None
        with self._resolver() as resolver:
            result = manager.apply(plan, resolver, actor="scheduler", now=self._clock)
        if result.error:
            logger.warning("Backup grooming stopped early (%s).", result.error.split(":", 1)[0])
        return result

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
            return self._manager().apply(entry.plan, resolver, actor="admin", now=self._clock)

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
        meta = self._meta.get(record.artifact_id) or {}
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
        meta = self._meta.get(record.artifact_id) or {}
        state = self.artifact_state(record)
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
        }

    def detail(self, artifact_id: str) -> dict[str, Any]:
        record = self.get(artifact_id)
        payload = self.serialize(record)
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
        meta = self._meta.get(record.artifact_id) or {}
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
        payload["last_verify"] = meta.get("last_verify")
        return payload

    def library(self) -> dict[str, Any]:
        records = self.catalog.list()
        storage: dict[str, dict[str, int]] = {}
        for record in records:
            bucket = storage.setdefault(record.location, {"config_bytes": 0, "full_bytes": 0, "count": 0})
            bucket[f"{record.backup_class}_bytes"] += record.size
            bucket["count"] += 1
        with self._state_lock:
            class_runs = dict(self._status["classes"])
            target_runs = dict(self._status["targets"])
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
            "detail": None,
            "running": self.running,
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
            "artifacts": [self.serialize(record) for record in reversed(records)],
            "storage": storage,
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
            with self._open_target(target.settings) as remote:
                size, digest = remote.get(record.name, local)
            # Never hand out bytes the catalogue did not record: a target (or an
            # on-path attacker for plain FTP/NFS) could substitute another backup.
            _require_match(record, size, digest)
            yield record, local
        finally:
            for child in workspace.iterdir():
                child.unlink(missing_ok=True)
            workspace.rmdir()

    def verify(self, artifact_id: str) -> dict[str, Any]:
        at = _iso(self._clock())
        ok = False
        record = self.get(artifact_id)
        try:
            with self.materialize(artifact_id):
                pass  # materialize re-hashes and compares with the catalogue
            self.catalog.mark_verified(artifact_id, sha256=record.sha256, size=record.size, now=self._clock())
            ok = True
            detail = "Readback matched the catalogued size and SHA-256."
        except ArchiveIntegrityError as exc:
            # A copy that no longer matches must stop counting as verified, so it is
            # neither offered for restore nor protected as the newest verified copy.
            self.catalog.mark_unverified(artifact_id)
            detail = str(exc)
        except ArtifactNotFoundError as exc:
            if record.location != LOCAL_LOCATION:
                raise
            self.catalog.mark_unverified(artifact_id)
            detail = str(exc)
        except Exception as exc:  # noqa: BLE001 - e.g. target unreachable; state unknown, keep flag
            detail = describe_error(exc)
        meta = self._meta.setdefault(artifact_id, {})
        meta["last_verify"] = {"at": at, "ok": ok, "detail": detail}
        self._save_meta()
        # Re-publish (or clear) verified_full from the catalog's current flag so
        # the history sidecar never suppresses its own copy after a failed
        # readback of the replacement artifact.
        self._write_status()
        return {"ok": ok, "artifact": self.serialize(self.get(artifact_id)), "detail": detail}

    def test_target(self, target_id: str) -> dict[str, Any]:
        target = self.policy.target(target_id)
        if target is None:
            raise ArtifactNotFoundError("Target not found.")
        try:
            with self._open_target(target.settings) as remote:
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


def _manifest_meta(manifest: Mapping[str, Any], groups: list[str]) -> dict[str, Any]:
    return {
        "app_version": manifest.get("app_version") if isinstance(manifest.get("app_version"), str) else None,
        "schema_version": manifest.get("schema_version") if isinstance(manifest.get("schema_version"), int) else None,
        "packaging": manifest.get("packaging") if isinstance(manifest.get("packaging"), str) else None,
        "groups": list(groups),
    }


def _require_match(record: ArtifactRecord, size: int, digest: str) -> None:
    if size != record.size or digest != record.sha256:
        raise ArchiveIntegrityError("Readback does not match the catalogued size and SHA-256.")


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


# -- config snapshot for the coalescer's content hash -------------------------------------


def snapshot_config_files(paths: Mapping[str, Path]) -> dict[str, Any]:
    """Parse each config document; a missing file is ``None`` (still hashed)."""

    snapshot: dict[str, Any] = {}
    for logical, path in paths.items():
        if not path.exists():
            snapshot[logical] = None
            continue
        text = path.read_text(encoding="utf-8")
        snapshot[logical] = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    return snapshot


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
