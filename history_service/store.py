from __future__ import annotations

import ctypes
import errno
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from history_service.domain import MetricSample, SlotEvent, SlotStateRecord
from history_service.migration_lock import history_write_lock
from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    activation_pending_path,
    path_entry_exists,
)
from history_service.segment_reader import MAX_HISTORY_QUERY_LIMIT, SegmentedHistoryReader

logger = logging.getLogger(__name__)
SQLITE_SHARED_DIR_MODE = 0o770
SQLITE_SHARED_FILE_MODE = 0o660
SQLITE_TEMP_STORE = "MEMORY"
SQLITE_CACHE_SIZE_KIB = 16384
SQLITE_CONNECT_TIMEOUT_SECONDS = 5.0
SQLITE_WRITE_LOCK_RETRY_ATTEMPTS = 2
SQLITE_WRITE_LOCK_RETRY_DELAY_SECONDS = 0.05
AT_FDCWD = -100
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
PRIVATE_REPLACEMENT_DIR_PREFIX = ".history-replacement-"
# PRAGMA user_version marker: once the disk-identity backfill has run against a database
# it is recorded here so later service starts (in every container sharing the file) skip
# the full-table UPDATE scans. Writers populate disk_identity_key on insert, so the
# backfill only ever has work to do for rows that predate the column.
DISK_IDENTITY_BACKFILL_USER_VERSION = 1
SEGMENTED_RETENTION_STATE_NAME = "segmented_retention"
SEGMENTED_RETENTION_STATES = frozenset({"ready", "claimed", "consumed"})


@dataclass(slots=True)
class SlotStateUpdate:
    record: SlotStateRecord
    observed_at: str
    events: list[SlotEvent] = field(default_factory=list)

# (name, SQL definition, add when missing from a legacy schema)
SLOT_STATE_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("system_id", "TEXT NOT NULL", False),
    ("system_label", "TEXT", False),
    ("enclosure_key", "TEXT NOT NULL", False),
    ("enclosure_id", "TEXT", False),
    ("enclosure_label", "TEXT", False),
    ("slot", "INTEGER NOT NULL", False),
    ("slot_label", "TEXT NOT NULL", False),
    ("present", "INTEGER NOT NULL", False),
    ("state", "TEXT", False),
    ("identify_active", "INTEGER NOT NULL", False),
    ("device_name", "TEXT", False),
    ("serial", "TEXT", False),
    ("model", "TEXT", False),
    ("gptid", "TEXT", False),
    ("persistent_id_label", "TEXT", True),
    ("disk_identity_key", "TEXT", True),
    ("logical_unit_id", "TEXT", True),
    ("sas_address", "TEXT", True),
    ("pool_name", "TEXT", False),
    ("vdev_name", "TEXT", False),
    ("health", "TEXT", False),
    ("topology_label", "TEXT", True),
    ("multipath_device", "TEXT", True),
    ("multipath_mode", "TEXT", True),
    ("multipath_state", "TEXT", True),
    ("multipath_lunid", "TEXT", True),
    ("multipath_primary_path", "TEXT", True),
    ("multipath_alternate_path", "TEXT", True),
    ("multipath_active_paths", "TEXT", True),
    ("multipath_passive_paths", "TEXT", True),
    ("multipath_failed_paths", "TEXT", True),
    ("multipath_other_paths", "TEXT", True),
    ("multipath_active_controllers", "TEXT", True),
    ("multipath_passive_controllers", "TEXT", True),
    ("multipath_failed_controllers", "TEXT", True),
    ("last_seen_at", "TEXT NOT NULL", False),
)
SLOT_STATE_COLUMN_NAMES = tuple(
    name for name, _definition, _optional in SLOT_STATE_COLUMNS
)
SLOT_STATE_OPTIONAL_COLUMNS: dict[str, str] = {
    name: definition
    for name, definition, optional in SLOT_STATE_COLUMNS
    if optional
}
SLOT_STATE_UPSERT_COLUMNS = SLOT_STATE_COLUMN_NAMES
SLOT_STATE_UPSERT_UPDATE_COLUMNS = tuple(
    name
    for name in SLOT_STATE_COLUMN_NAMES
    if name not in {"system_id", "enclosure_key", "slot"}
)
SLOT_STATE_ADOPTION_PROJECTION = ("?", "?", *SLOT_STATE_COLUMN_NAMES[2:])

_SLOT_STATE_SCHEMA_COLUMNS_SQL = ",\n".join(
    f"    {name} {definition}"
    for name, definition, _optional in SLOT_STATE_COLUMNS
)
_SLOT_STATE_UPSERT_COLUMNS_SQL = ",\n".join(
    f"                {name}" for name in SLOT_STATE_UPSERT_COLUMNS
)
_SLOT_STATE_UPSERT_VALUES_SQL = ", ".join("?" for _name in SLOT_STATE_UPSERT_COLUMNS)
_SLOT_STATE_UPSERT_UPDATE_SQL = ",\n".join(
    f"                {name} = excluded.{name}"
    for name in SLOT_STATE_UPSERT_UPDATE_COLUMNS
)
SLOT_STATE_UPSERT_SQL = f"""
            INSERT INTO slot_state_current (
{_SLOT_STATE_UPSERT_COLUMNS_SQL}
            ) VALUES ({_SLOT_STATE_UPSERT_VALUES_SQL})
            ON CONFLICT(system_id, enclosure_key, slot) DO UPDATE SET
{_SLOT_STATE_UPSERT_UPDATE_SQL}
            """

_SLOT_STATE_ADOPTION_COLUMNS_SQL = ",\n".join(
    f"                        {name}" for name in SLOT_STATE_COLUMN_NAMES
)
_SLOT_STATE_ADOPTION_PROJECTION_SQL = ",\n".join(
    f"                        {projection}" for projection in SLOT_STATE_ADOPTION_PROJECTION
)
SLOT_STATE_ADOPTION_SQL = f"""
                    INSERT OR IGNORE INTO slot_state_current (
{_SLOT_STATE_ADOPTION_COLUMNS_SQL}
                    )
                    SELECT
{_SLOT_STATE_ADOPTION_PROJECTION_SQL}
                    FROM slot_state_current
                    WHERE system_id = ?
                    """


def _slot_state_record_values(record: SlotStateRecord, observed_at: str) -> tuple[Any, ...]:
    values = []
    for column_name in SLOT_STATE_COLUMN_NAMES:
        value = observed_at if column_name == "last_seen_at" else getattr(record, column_name)
        if column_name in {"present", "identify_active"}:
            value = int(value)
        values.append(value)
    return tuple(values)


SLOT_EVENT_OPTIONAL_COLUMNS: dict[str, str] = {
    "gptid": "TEXT",
    "persistent_id_label": "TEXT",
    "disk_identity_key": "TEXT",
    "logical_unit_id": "TEXT",
    "sas_address": "TEXT",
}

METRIC_SAMPLE_OPTIONAL_COLUMNS: dict[str, str] = {
    "gptid": "TEXT",
    "persistent_id_label": "TEXT",
    "disk_identity_key": "TEXT",
    "logical_unit_id": "TEXT",
    "sas_address": "TEXT",
}

ROLLUP_COUNTER_METRICS = ("bytes_read", "bytes_written", "power_on_hours")
_ROLLUP_COUNTER_METRICS_SQL = ", ".join(f"'{metric_name}'" for metric_name in ROLLUP_COUNTER_METRICS)
ROLLUP_TO_SAMPLE_PROJECTION = f"""                    NULL AS id,
                    CASE
                        WHEN metric_name IN ({_ROLLUP_COUNTER_METRICS_SQL})
                        THEN last_observed_at
                        ELSE bucket_start
                    END AS observed_at,
                    system_id,
                    system_label,
                    enclosure_key,
                    enclosure_id,
                    enclosure_label,
                    slot,
                    slot_label,
                    metric_name,
                    NULL AS value_integer,
                    CASE
                        WHEN metric_name IN ({_ROLLUP_COUNTER_METRICS_SQL})
                        THEN last_value
                        ELSE value_sum / sample_count
                    END AS value_real,
                    device_name,
                    serial,
                    model,
                    state,
                    gptid,
                    persistent_id_label,
                    NULLIF(disk_identity_key, '') AS disk_identity_key,
                    logical_unit_id,
                    sas_address,
                    bucket_seconds AS rollup_seconds,
                    sample_count,
                    value_min,
                    value_max"""


def _indent_sql(sql: str, spaces: int) -> str:
    prefix = " " * spaces
    return prefix + sql.replace("\n", f"\n{prefix}")


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS slot_state_current (
{_SLOT_STATE_SCHEMA_COLUMNS_SQL},
    PRIMARY KEY (system_id, enclosure_key, slot)
);

CREATE TABLE IF NOT EXISTS slot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_value TEXT,
    current_value TEXT,
    device_name TEXT,
    serial TEXT,
    details_json TEXT NOT NULL,
    gptid TEXT,
    persistent_id_label TEXT,
    disk_identity_key TEXT,
    logical_unit_id TEXT,
    sas_address TEXT
);

CREATE TABLE IF NOT EXISTS metric_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value_integer INTEGER,
    value_real REAL,
    device_name TEXT,
    serial TEXT,
    model TEXT,
    state TEXT,
    gptid TEXT,
    persistent_id_label TEXT,
    disk_identity_key TEXT,
    logical_unit_id TEXT,
    sas_address TEXT
);

CREATE TABLE IF NOT EXISTS metric_rollups (
    bucket_start TEXT NOT NULL,
    bucket_seconds INTEGER NOT NULL,
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    value_sum REAL NOT NULL,
    value_min REAL NOT NULL,
    value_max REAL NOT NULL,
    last_value REAL NOT NULL,
    last_observed_at TEXT NOT NULL,
    device_name TEXT,
    serial TEXT,
    model TEXT,
    state TEXT,
    gptid TEXT,
    persistent_id_label TEXT,
    disk_identity_key TEXT NOT NULL DEFAULT '',
    logical_unit_id TEXT,
    sas_address TEXT,
    PRIMARY KEY (
        bucket_seconds,
        bucket_start,
        system_id,
        enclosure_key,
        slot,
        metric_name,
        disk_identity_key
    )
);

CREATE TABLE IF NOT EXISTS history_table_counts (
    table_name TEXT PRIMARY KEY,
    row_count INTEGER NOT NULL CHECK (row_count >= 0)
);

CREATE TABLE IF NOT EXISTS history_maintenance_state (
    name TEXT PRIMARY KEY,
    backup_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'claimed', 'consumed'))
);

CREATE TRIGGER IF NOT EXISTS count_slot_events_insert
AFTER INSERT ON slot_events BEGIN
    UPDATE history_table_counts
    SET row_count = row_count + 1
    WHERE table_name = 'slot_events';
END;

CREATE TRIGGER IF NOT EXISTS count_slot_events_delete
AFTER DELETE ON slot_events BEGIN
    UPDATE history_table_counts
    SET row_count = row_count - 1
    WHERE table_name = 'slot_events';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_samples_insert
AFTER INSERT ON metric_samples BEGIN
    UPDATE history_table_counts
    SET row_count = row_count + 1
    WHERE table_name = 'metric_samples';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_samples_delete
AFTER DELETE ON metric_samples BEGIN
    UPDATE history_table_counts
    SET row_count = row_count - 1
    WHERE table_name = 'metric_samples';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_rollups_insert
AFTER INSERT ON metric_rollups BEGIN
    UPDATE history_table_counts
    SET row_count = row_count + 1
    WHERE table_name = 'metric_rollups';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_rollups_delete
AFTER DELETE ON metric_rollups BEGIN
    UPDATE history_table_counts
    SET row_count = row_count - 1
    WHERE table_name = 'metric_rollups';
END;

CREATE INDEX IF NOT EXISTS idx_slot_events_scope
    ON slot_events (system_id, enclosure_key, slot, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_metric_samples_scope
    ON metric_samples (system_id, enclosure_key, slot, metric_name, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_slot_events_observed_at
    ON slot_events (observed_at, id);

CREATE INDEX IF NOT EXISTS idx_metric_samples_observed_at
    ON metric_samples (observed_at, id);

CREATE INDEX IF NOT EXISTS idx_metric_rollups_scope
    ON metric_rollups (
        system_id,
        enclosure_key,
        slot,
        metric_name,
        bucket_seconds,
        bucket_start DESC
    );

CREATE INDEX IF NOT EXISTS idx_metric_rollups_retention
    ON metric_rollups (bucket_seconds, bucket_start);
"""


class HistoryStore:
    def __init__(
        self,
        file_path: str,
        *,
        recover_unreadable_database: bool = True,
        permission_repair_enabled: bool = False,
        shared_dir_mode: int = SQLITE_SHARED_DIR_MODE,
        shared_file_mode: int = SQLITE_SHARED_FILE_MODE,
        segment_catalog_path: str | Path | None = None,
        initialize: bool = True,
    ) -> None:
        self.file_path = Path(file_path)
        self.segment_catalog_path = (
            Path(segment_catalog_path).absolute()
            if segment_catalog_path is not None
            else None
        )
        self.recover_unreadable_database = bool(recover_unreadable_database)
        self.permission_repair_enabled = bool(permission_repair_enabled)
        self._initialize_enabled = bool(initialize)
        self.shared_dir_mode = int(shared_dir_mode)
        self.shared_file_mode = int(shared_file_mode)
        for label, mode in (
            ("shared directory", self.shared_dir_mode),
            ("shared file", self.shared_file_mode),
        ):
            if mode < 0 or mode > 0o777:
                raise ValueError(f"History {label} mode must be between 0000 and 0777.")
            if mode & 0o002:
                raise ValueError(f"History {label} mode must not be world-writable.")
        self._ensure_database_parent()
        self._lock = threading.Lock()
        self._journal_mode_lock = threading.Lock()
        self._journal_mode_identity: tuple[int, int] | None = None
        self._segment_reader_lock = threading.Lock()
        self._segment_reader_identity: tuple[int, int, int, int] | None = None
        self._segment_reader_cache: SegmentedHistoryReader | None = None
        if self._initialize_enabled:
            with history_write_lock(self.file_path, blocking=False):
                self._require_no_pending_lifecycle_markers()
                self._initialize(migration_lock_held=True)

    def _ensure_database_parent(self) -> None:
        try:
            self.file_path.parent.mkdir(
                mode=self.shared_dir_mode,
                parents=True,
            )
        except FileExistsError:
            self._normalize_shared_path_permissions(self.file_path.parent, is_dir=True)
            return
        self._set_shared_path_permissions(self.file_path.parent, is_dir=True)

    def _require_no_pending_lifecycle_markers(self) -> None:
        """
        Refuse to touch the hot database while a rotation, migration, or
        segmented restore is pending.

        The markers are honoured by the segmented reader, but `_initialize`
        (schema executescript, column adds, table-count sync, journal-mode
        switch) and the collector's writes used to run regardless. A service
        restart during a pending journal then mutated the hot file, and
        `rotate --recover` could match neither the prior nor the candidate
        digest (issue #174). Plain reads and the segmented-retention claim
        writes reached `_connect` without this check and rewrote the journal
        header the same way (issue #279), so `_connect` now calls it for every
        connection. Raise the same error type the migration lock raises so
        callers keep one failure path.
        """
        if path_entry_exists(activation_pending_path(self.file_path)):
            raise sqlite3.OperationalError(
                "Segmented history activation is pending; refusing to open the history "
                "database until the pending rotation or restore is recovered."
            )
        if self.segment_catalog_path is None:
            return
        pending_path = self.segment_catalog_path.parent / MIGRATION_PENDING_MARKER
        if path_entry_exists(pending_path):
            raise sqlite3.OperationalError(
                "Segmented history migration recovery is pending; refusing to open the history "
                "database until the pending migration is recovered."
            )

    def _segmented_reader(self) -> SegmentedHistoryReader | None:
        if self.segment_catalog_path is None:
            return None
        if path_entry_exists(activation_pending_path(self.file_path)):
            raise ValueError("Segmented history activation is pending.")
        pending_path = self.segment_catalog_path.parent / MIGRATION_PENDING_MARKER
        if path_entry_exists(pending_path):
            raise ValueError("Segmented history migration recovery is pending.")
        with self._segment_reader_lock:
            if path_entry_exists(pending_path):
                raise ValueError("Segmented history migration recovery is pending.")
            metadata = os.stat(self.segment_catalog_path, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Segmented history catalog must be a regular file.")
            identity = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
            )
            if self._segment_reader_cache is not None and identity == self._segment_reader_identity:
                return self._segment_reader_cache
            reader = SegmentedHistoryReader.from_catalog(
                hot_path=self.file_path,
                catalog_path=self.segment_catalog_path,
            )
            self._segment_reader_cache = reader
            self._segment_reader_identity = identity
            return reader

    def _require_unsegmented_operation(self, operation: str) -> None:
        if self.segment_catalog_path is not None:
            raise ValueError(
                f"History {operation} is unavailable while segmented history is active."
            )

    @staticmethod
    def _normalize_retention_backup_at(value: datetime | str) -> tuple[datetime, str]:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("History retention backup timestamp is invalid.") from exc
        else:
            raise ValueError("History retention backup timestamp is invalid.")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("History retention backup timestamp is invalid.")
        normalized = parsed.astimezone(timezone.utc)
        return normalized, normalized.isoformat()

    @contextmanager
    def segmented_retention_write_lock(self) -> Iterator[None]:
        with history_write_lock(self.file_path, blocking=False):
            yield

    @contextmanager
    def _locked_write_connection(
        self,
        *,
        migration_lock_held: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        lock_context = (
            nullcontext()
            if migration_lock_held
            else history_write_lock(self.file_path, blocking=False)
        )
        with lock_context:
            with closing(self._connect(migration_lock_held=True)) as connection:
                yield connection

    def claim_segmented_retention_backup(
        self,
        backup_at: datetime | str,
        *,
        migration_lock_held: bool = False,
    ) -> bool:
        candidate, serialized = self._normalize_retention_backup_at(backup_at)
        with self._locked_write_connection(
            migration_lock_held=migration_lock_held
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT backup_at, state
                    FROM history_maintenance_state
                    WHERE name = ?
                    """,
                    (SEGMENTED_RETENTION_STATE_NAME,),
                ).fetchone()
                if row is not None:
                    previous, _ = self._normalize_retention_backup_at(str(row["backup_at"]))
                    state = str(row["state"])
                    if state not in SEGMENTED_RETENTION_STATES:
                        raise ValueError("History retention authorization state is invalid.")
                    if candidate < previous or (candidate == previous and state != "ready"):
                        connection.rollback()
                        return False
                connection.execute(
                    """
                    INSERT INTO history_maintenance_state (name, backup_at, state)
                    VALUES (?, ?, 'claimed')
                    ON CONFLICT (name) DO UPDATE SET
                        backup_at = excluded.backup_at,
                        state = excluded.state
                    """,
                    (SEGMENTED_RETENTION_STATE_NAME, serialized),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def finish_segmented_retention_backup(
        self,
        backup_at: datetime | str,
        *,
        has_more: bool,
        migration_lock_held: bool = False,
    ) -> None:
        _, serialized = self._normalize_retention_backup_at(backup_at)
        next_state = "ready" if has_more else "consumed"
        with self._locked_write_connection(
            migration_lock_held=migration_lock_held
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE history_maintenance_state
                    SET state = ?
                    WHERE name = ? AND backup_at = ? AND state = 'claimed'
                    """,
                    (next_state, SEGMENTED_RETENTION_STATE_NAME, serialized),
                )
                if cursor.rowcount != 1:
                    raise ValueError("History retention authorization claim changed.")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def release_segmented_retention_backup(
        self,
        backup_at: datetime | str,
        *,
        migration_lock_held: bool = False,
    ) -> None:
        _, serialized = self._normalize_retention_backup_at(backup_at)
        with self._locked_write_connection(
            migration_lock_held=migration_lock_held
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE history_maintenance_state
                    SET state = 'ready'
                    WHERE name = ? AND backup_at = ? AND state = 'claimed'
                    """,
                    (SEGMENTED_RETENTION_STATE_NAME, serialized),
                )
                if cursor.rowcount != 1:
                    raise ValueError("History retention authorization claim changed.")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def run_segmented_retention(
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

    def _connect(self, *, migration_lock_held: bool = False) -> sqlite3.Connection:
        lock_context = (
            nullcontext()
            if migration_lock_held
            else history_write_lock(self.file_path, blocking=True)
        )
        with lock_context:
            return self._connect_locked()

    def _connect_locked(self) -> sqlite3.Connection:
        """Open and fully configure a connection while the lifecycle lock is held."""

        self._require_no_pending_lifecycle_markers()
        connection = sqlite3.connect(
            self.file_path,
            timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA temp_store={SQLITE_TEMP_STORE}")
            connection.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_SIZE_KIB}")
            self._ensure_journal_mode_locked(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _ensure_journal_mode_locked(self, connection: sqlite3.Connection) -> None:
        try:
            stat_result = self.file_path.stat()
            identity = (int(stat_result.st_dev), int(stat_result.st_ino))
        except FileNotFoundError:
            identity = None
        with self._journal_mode_lock:
            if identity is not None and identity == self._journal_mode_identity:
                return
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if not self._is_journal_mode_fallback_error(exc):
                    raise
                logger.warning(
                    "History database %s could not enable WAL mode; continuing with the existing journal mode. Error: %s",
                    self.file_path,
                    exc,
                )
                return
            self._journal_mode_identity = identity

    def database_size_bytes(self) -> int:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.database_size_bytes()
        total = 0
        for path in (
            self.file_path,
            Path(f"{self.file_path}-wal"),
            Path(f"{self.file_path}-shm"),
        ):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total

    def _initialize(self, *, migration_lock_held: bool = False) -> None:
        self._normalize_database_permissions()
        try:
            self._initialize_schema_and_permissions(migration_lock_held=migration_lock_held)
        except sqlite3.OperationalError as exc:
            if self._is_readonly_database_error(exc) and self._attempt_readonly_database_repair(exc):
                self._initialize_schema_and_permissions(migration_lock_held=migration_lock_held)
                return
            if (
                not self.recover_unreadable_database
                or not self._should_recover_database(exc)
            ):
                raise
            broken_path = self._quarantine_database()
            logger.warning(
                "History database %s was unreadable; moved it to %s and created a fresh database. Error: %s",
                self.file_path,
                broken_path,
                exc,
            )
            self._initialize_schema_and_permissions(migration_lock_held=migration_lock_held)
        except sqlite3.Error as exc:
            if (
                not self.recover_unreadable_database
                or not self._should_recover_database(exc)
            ):
                raise
            broken_path = self._quarantine_database()
            logger.warning(
                "History database %s was unreadable; moved it to %s and created a fresh database. Error: %s",
                self.file_path,
                broken_path,
                exc,
            )
            self._initialize_schema_and_permissions(migration_lock_held=migration_lock_held)

    def _initialize_schema_and_permissions(self, *, migration_lock_held: bool = False) -> None:
        self._create_database_file_for_shared_access()
        self._initialize_schema(migration_lock_held=migration_lock_held)
        self._normalize_database_permissions()

    def _create_database_file_for_shared_access(self) -> None:
        # SQLite derives new WAL/SHM ownership and modes from the main database.
        # Publish the fresh database before the first SQLite connection opens it.
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.file_path, flags, self.shared_file_mode)
        except FileExistsError:
            return
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"History shared database creation refuses non-regular path {self.file_path}."
                )
            if stat.S_IMODE(metadata.st_mode) != self.shared_file_mode:
                os.fchmod(descriptor, self.shared_file_mode)
        finally:
            os.close(descriptor)

    def _initialize_schema(self, *, migration_lock_held: bool = False) -> None:
        with closing(self._connect(migration_lock_held=migration_lock_held)) as connection:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            connection.executescript(SCHEMA)
            self._ensure_slot_state_columns(connection)
            self._ensure_slot_event_columns(connection)
            self._ensure_metric_sample_columns(connection)
            self._backfill_disk_identity_keys_once(connection)
            self._ensure_identity_indexes(connection)
            self._synchronize_table_counts(connection)
            connection.commit()

    @staticmethod
    def _synchronize_table_counts(connection: sqlite3.Connection) -> None:
        for table_name in ("slot_events", "metric_samples", "metric_rollups"):
            connection.execute(
                f"""
                INSERT INTO history_table_counts (table_name, row_count)
                VALUES (?, (SELECT COUNT(*) FROM {table_name}))
                ON CONFLICT (table_name) DO UPDATE SET
                    row_count = excluded.row_count
                """,
                (table_name,),
            )

    @staticmethod
    def _backfill_disk_identity_keys_once(connection: sqlite3.Connection) -> None:
        row = connection.execute("PRAGMA user_version").fetchone()
        current_version = int(row[0]) if row and row[0] is not None else 0
        if current_version >= DISK_IDENTITY_BACKFILL_USER_VERSION:
            return
        HistoryStore._backfill_disk_identity_keys(connection)
        connection.execute(f"PRAGMA user_version = {int(DISK_IDENTITY_BACKFILL_USER_VERSION)}")

    @staticmethod
    def _ensure_slot_state_columns(connection: sqlite3.Connection) -> None:
        HistoryStore._ensure_columns(connection, "slot_state_current", SLOT_STATE_OPTIONAL_COLUMNS)

    @staticmethod
    def _ensure_slot_event_columns(connection: sqlite3.Connection) -> None:
        HistoryStore._ensure_columns(connection, "slot_events", SLOT_EVENT_OPTIONAL_COLUMNS)

    @staticmethod
    def _ensure_metric_sample_columns(connection: sqlite3.Connection) -> None:
        HistoryStore._ensure_columns(connection, "metric_samples", METRIC_SAMPLE_OPTIONAL_COLUMNS)

    @staticmethod
    def _backfill_disk_identity_keys(connection: sqlite3.Connection) -> None:
        for table_name in ("slot_state_current", "slot_events", "metric_samples"):
            connection.execute(
                f"""
                UPDATE {table_name}
                SET disk_identity_key =
                    lower(trim(serial)) || '|' ||
                    lower(trim(coalesce(nullif(persistent_id_label, ''), 'unknown'))) || '|' ||
                    lower(trim(gptid))
                WHERE (disk_identity_key IS NULL OR trim(disk_identity_key) = '')
                  AND serial IS NOT NULL
                  AND trim(serial) <> ''
                  AND gptid IS NOT NULL
                  AND trim(gptid) <> ''
                """
            )

    @staticmethod
    def _ensure_identity_indexes(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_slot_state_disk_identity
                ON slot_state_current (disk_identity_key)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_samples_disk_identity
                ON metric_samples (disk_identity_key, metric_name, observed_at DESC)
            """
        )

    @staticmethod
    def _ensure_columns(
        connection: sqlite3.Connection,
        table_name: str,
        optional_columns: dict[str, str],
    ) -> None:
        existing_columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, column_type in optional_columns.items():
            if column_name in existing_columns:
                continue
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )

    @staticmethod
    def _should_recover_database(exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return any(
            fragment in message
            for fragment in (
                "file is not a database",
                "database disk image is malformed",
            )
        )

    def _quarantine_database(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        broken_path = self.file_path.with_name(f"{self.file_path.name}.broken-{timestamp}")
        self.file_path.replace(broken_path)
        for suffix in ("-shm", "-wal"):
            sidecar_path = Path(f"{self.file_path}{suffix}")
            if not sidecar_path.exists():
                continue
            sidecar_path.replace(broken_path.with_name(f"{broken_path.name}{suffix}"))
        return broken_path

    def create_backup(
        self,
        backup_dir: str | Path,
        *,
        snapshot_label: str | None = None,
        retention_count: int = 28,
        long_term_backup_dir: str | Path | None = None,
        weekly_retention_count: int = 0,
        monthly_retention_count: int = 0,
    ) -> Path | None:
        self._require_unsegmented_operation("v1 backup")
        backup_root = Path(backup_dir)
        backup_root.mkdir(parents=True, exist_ok=True)
        self._normalize_shared_path_permissions(backup_root, is_dir=True)
        backup_name = f"{self.file_path.stem}-{self._backup_stamp(snapshot_label)}.sqlite3"
        final_path = backup_root / backup_name
        temp_fd, temp_path = self._create_private_replacement_file(
            backup_root,
            prefix=f".{backup_name}.",
            suffix=".tmp",
        )
        temp_metadata = os.fstat(temp_fd)

        with self._lock:
            try:
                with closing(self._connect()) as source_connection, closing(
                    sqlite3.connect(f"/proc/self/fd/{temp_fd}")
                ) as backup_connection:
                    backup_connection.execute("PRAGMA journal_mode=MEMORY")
                    source_connection.backup(backup_connection)
                    backup_connection.commit()
                publish_descriptor = temp_fd
                temp_fd = None
                self._publish_replacement(
                    temp_path,
                    final_path,
                    temp_descriptor=publish_descriptor,
                )
                self._prune_backup_snapshots(backup_root, retention_count)
                try:
                    self._promote_long_term_backups(
                        final_path,
                        snapshot_label=snapshot_label,
                        long_term_backup_dir=long_term_backup_dir,
                        weekly_retention_count=weekly_retention_count,
                        monthly_retention_count=monthly_retention_count,
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort archival path should not break local backup rotation.
                    logger.warning("History long-term backup promotion failed for %s: %s", final_path, exc)
            finally:
                if temp_fd is not None:
                    os.close(temp_fd)
                self._discard_owned_path(temp_path, temp_metadata)

        return final_path

    def latest_backup_snapshot_at(self, backup_dir: str | Path) -> datetime | None:
        backup_root = Path(backup_dir)
        if not backup_root.exists():
            return None
        snapshots = list(backup_root.glob(f"{self.file_path.stem}-*.sqlite3"))
        if not snapshots:
            return None
        latest = max(snapshots, key=lambda candidate: candidate.stat().st_mtime)
        return datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)

    def _restore_backup_locked(self, source_path: str | Path) -> None:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Backup source {source} does not exist.")

        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._normalize_shared_path_permissions(self.file_path.parent, is_dir=True)
        ownership_source = self.file_path if self.file_path.exists() else self.file_path.parent
        ownership_metadata = os.stat(ownership_source, follow_symlinks=False)
        temp_fd, temp_path = self._create_private_replacement_file(
            self.file_path.parent,
            prefix=f".{self.file_path.name}.",
            suffix=".restore",
        )
        temp_metadata = os.fstat(temp_fd)
        try:
            with self._lock:
                with closing(sqlite3.connect(source)) as source_connection, closing(
                    sqlite3.connect(f"/proc/self/fd/{temp_fd}")
                ) as restore_connection:
                    restore_connection.execute("PRAGMA journal_mode=MEMORY")
                    source_connection.backup(restore_connection)
                    restore_connection.commit()

                os.fchown(temp_fd, ownership_metadata.st_uid, ownership_metadata.st_gid)
                publish_descriptor = temp_fd
                temp_fd = None
                self._publish_replacement(
                    temp_path,
                    self.file_path,
                    temp_descriptor=publish_descriptor,
                )
                for suffix in ("-shm", "-wal"):
                    Path(f"{self.file_path}{suffix}").unlink(missing_ok=True)
                self._normalize_database_permissions()
                self._journal_mode_identity = None
                if self._initialize_enabled:
                    self._initialize_schema(migration_lock_held=True)
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            self._discard_owned_path(temp_path, temp_metadata)

    def restore_backup(self, source_path: str | Path) -> None:
        self._require_unsegmented_operation("v1 restore")
        with history_write_lock(self.file_path, blocking=False):
            self._restore_backup_locked(source_path)

    @staticmethod
    def _backup_stamp(snapshot_label: str | None) -> str:
        observed_at = HistoryStore._parse_snapshot_label(snapshot_label)
        return observed_at.strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def _parse_snapshot_label(snapshot_label: str | None) -> datetime:
        if snapshot_label:
            try:
                observed_at = datetime.fromisoformat(snapshot_label)
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                return observed_at.astimezone(timezone.utc)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def _prune_backup_snapshots(self, backup_root: Path, retention_count: int) -> None:
        if retention_count < 1:
            return
        snapshots = sorted(
            backup_root.glob(f"{self.file_path.stem}-*.sqlite3"),
            key=lambda candidate: candidate.name,
            reverse=True,
        )
        for stale_path in snapshots[retention_count:]:
            stale_path.unlink(missing_ok=True)

    def _promote_long_term_backups(
        self,
        source_backup_path: Path,
        *,
        snapshot_label: str | None,
        long_term_backup_dir: str | Path | None,
        weekly_retention_count: int,
        monthly_retention_count: int,
    ) -> None:
        if not long_term_backup_dir:
            return

        observed_at = self._parse_snapshot_label(snapshot_label)
        long_term_root = Path(long_term_backup_dir)
        long_term_root.mkdir(parents=True, exist_ok=True)
        self._normalize_shared_path_permissions(long_term_root, is_dir=True)

        if weekly_retention_count > 0:
            iso_year, iso_week, _ = observed_at.isocalendar()
            weekly_path = long_term_root / "weekly" / f"{self.file_path.stem}-weekly-{iso_year}-W{iso_week:02d}.sqlite3"
            self._refresh_backup_copy(source_backup_path, weekly_path)
            self._prune_named_backups(
                weekly_path.parent,
                f"{self.file_path.stem}-weekly-*.sqlite3",
                weekly_retention_count,
            )

        if monthly_retention_count > 0:
            monthly_path = long_term_root / "monthly" / f"{self.file_path.stem}-monthly-{observed_at:%Y-%m}.sqlite3"
            self._refresh_backup_copy(source_backup_path, monthly_path)
            self._prune_named_backups(
                monthly_path.parent,
                f"{self.file_path.stem}-monthly-*.sqlite3",
                monthly_retention_count,
            )

    def _refresh_backup_copy(self, source_backup_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self._normalize_shared_path_permissions(target_path.parent, is_dir=True)
        temp_fd, temp_path = self._create_private_replacement_file(
            target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
        )
        temp_metadata = os.fstat(temp_fd)
        try:
            self._copy_file_to_descriptor(source_backup_path, temp_fd)
            publish_descriptor = temp_fd
            temp_fd = None
            self._publish_replacement(
                temp_path,
                target_path,
                temp_descriptor=publish_descriptor,
            )
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            self._discard_owned_path(temp_path, temp_metadata)

    @staticmethod
    def _prune_named_backups(backup_root: Path, pattern: str, retention_count: int) -> None:
        if retention_count < 1 or not backup_root.exists():
            return
        snapshots = sorted(
            backup_root.glob(pattern),
            key=lambda candidate: candidate.name,
            reverse=True,
        )
        for stale_path in snapshots[retention_count:]:
            stale_path.unlink(missing_ok=True)

    def get_slot_state(self, system_id: str, enclosure_id: str | None, slot: int) -> SlotStateRecord | None:
        enclosure_key = enclosure_id or ""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM slot_state_current
                WHERE system_id = ? AND enclosure_key = ? AND slot = ?
                """,
                (system_id, enclosure_key, slot),
            ).fetchone()
        return self._row_to_slot_state(row) if row else None

    def upsert_slot_state(self, record: SlotStateRecord, observed_at: str) -> None:
        self._execute_write(lambda connection: self._upsert_slot_state_row(connection, record, observed_at))

    @staticmethod
    def _upsert_slot_state_row(connection: sqlite3.Connection, record: SlotStateRecord, observed_at: str) -> None:
        connection.execute(
            SLOT_STATE_UPSERT_SQL,
            _slot_state_record_values(record, observed_at),
        )

    def insert_events(self, events: list[SlotEvent]) -> None:
        if not events:
            return
        self._execute_write(lambda connection: self._insert_event_rows(connection, events))

    def get_slot_states(self, system_id: str, enclosure_id: str | None) -> dict[int, SlotStateRecord]:
        """Load every current slot-state row for one scope with a single connection."""

        enclosure_key = enclosure_id or ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM slot_state_current
                WHERE system_id = ? AND enclosure_key = ?
                """,
                (system_id, enclosure_key),
            ).fetchall()
        return {int(row["slot"]): self._row_to_slot_state(row) for row in rows}

    def record_slot_updates(self, updates: list[SlotStateUpdate]) -> None:
        """Apply a batch of slot-state upserts (and their events) in one connection/transaction.

        Replaces the per-slot open/PRAGMA/commit/close cycle the collector used to pay
        three times per slot per pass. Event rows for a slot are written before its
        state row, matching the single-call ordering.
        """

        if not updates:
            return

        def apply(connection: sqlite3.Connection) -> None:
            for update in updates:
                if update.events:
                    self._insert_event_rows(connection, list(update.events))
                self._upsert_slot_state_row(connection, update.record, update.observed_at)

        self._execute_write(apply)

    @staticmethod
    def _insert_event_rows(connection: sqlite3.Connection, events: list[SlotEvent]) -> None:
        if not events:
            return
        connection.executemany(
            """
            INSERT INTO slot_events (
                observed_at,
                system_id,
                system_label,
                enclosure_key,
                enclosure_id,
                enclosure_label,
                slot,
                slot_label,
                event_type,
                previous_value,
                current_value,
                device_name,
                serial,
                details_json,
                gptid,
                persistent_id_label,
                disk_identity_key,
                logical_unit_id,
                sas_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.observed_at,
                    item.system_id,
                    item.system_label,
                    item.enclosure_key,
                    item.enclosure_id,
                    item.enclosure_label,
                    item.slot,
                    item.slot_label,
                    item.event_type,
                    item.previous_value,
                    item.current_value,
                    item.device_name,
                    item.serial,
                    item.details_json,
                    item.gptid,
                    item.persistent_id_label,
                    item.disk_identity_key,
                    item.logical_unit_id,
                    item.sas_address,
                )
                for item in events
            ],
        )

    def insert_metric_samples(self, samples: list[MetricSample]) -> None:
        if not samples:
            return
        self._execute_write(
            lambda connection: connection.executemany(
                """
                INSERT INTO metric_samples (
                    observed_at,
                    system_id,
                    system_label,
                    enclosure_key,
                    enclosure_id,
                    enclosure_label,
                    slot,
                    slot_label,
                    metric_name,
                    value_integer,
                    value_real,
                    device_name,
                    serial,
                    model,
                    state,
                    gptid,
                    persistent_id_label,
                    disk_identity_key,
                    logical_unit_id,
                    sas_address
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.observed_at,
                        item.system_id,
                        item.system_label,
                        item.enclosure_key,
                        item.enclosure_id,
                        item.enclosure_label,
                        item.slot,
                        item.slot_label,
                        item.metric_name,
                        item.value_integer,
                        item.value_real,
                        item.device_name,
                        item.serial,
                        item.model,
                        item.state,
                        item.gptid,
                        item.persistent_id_label,
                        item.disk_identity_key,
                        item.logical_unit_id,
                        item.sas_address,
                    )
                    for item in samples
                ],
            )
        )

    def maintain_retention(
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

        normalized_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        normalized_now = normalized_now.astimezone(timezone.utc)
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
                    ),
                    migration_lock_held=migration_lock_held,
                )
            except Exception as exc:
                summary["has_more"] = True
                summary["total_rows_removed"] = self._retention_total_rows_removed(summary)
                setattr(exc, "retention_summary", dict(summary))
                raise
            summary["batches_completed"] += 1
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

        summary["total_rows_removed"] = self._retention_total_rows_removed(summary)
        return summary

    @staticmethod
    def _retention_total_rows_removed(summary: dict[str, Any]) -> int:
        return sum(
            int(summary[key])
            for key in (
                "metric_samples_removed",
                "events_removed",
                "hourly_rollups_removed",
                "daily_rollups_removed",
            )
        )

    @staticmethod
    def _retention_cutoff(now: datetime, days: int) -> str | None:
        if days <= 0:
            return None
        return (now - timedelta(days=days)).isoformat()

    @classmethod
    def _maintain_retention_batch(
        cls,
        connection: sqlite3.Connection,
        *,
        cutoffs: dict[str, str | None],
        batch_size: int,
    ) -> dict[str, Any]:
        metric_ids = cls._retention_row_ids(
            connection,
            table_name="metric_samples",
            timestamp_column="observed_at",
            cutoff=cutoffs["metric"],
            batch_size=batch_size,
        )
        if metric_ids:
            cls._roll_up_metric_rows(connection, metric_ids, bucket_seconds=3600)
            cls._roll_up_metric_rows(connection, metric_ids, bucket_seconds=86400)
            metric_samples_removed = cls._delete_rows_by_id(connection, "metric_samples", metric_ids)
        else:
            metric_samples_removed = 0

        event_ids = cls._retention_row_ids(
            connection,
            table_name="slot_events",
            timestamp_column="observed_at",
            cutoff=cutoffs["event"],
            batch_size=batch_size,
        )
        events_removed = cls._delete_rows_by_id(connection, "slot_events", event_ids)
        hourly_rollups_removed = cls._delete_rollup_batch(
            connection,
            bucket_seconds=3600,
            cutoff=cutoffs["hourly"],
            batch_size=batch_size,
        )
        daily_rollups_removed = cls._delete_rollup_batch(
            connection,
            bucket_seconds=86400,
            cutoff=cutoffs["daily"],
            batch_size=batch_size,
        )
        has_more = any(
            (
                cls._retention_rows_exist(connection, "metric_samples", "observed_at", cutoffs["metric"]),
                cls._retention_rows_exist(connection, "slot_events", "observed_at", cutoffs["event"]),
                cls._rollups_exist(connection, 3600, cutoffs["hourly"]),
                cls._rollups_exist(connection, 86400, cutoffs["daily"]),
            )
        )
        return {
            "metric_samples_removed": metric_samples_removed,
            "events_removed": events_removed,
            "hourly_rollups_removed": hourly_rollups_removed,
            "daily_rollups_removed": daily_rollups_removed,
            "has_more": has_more,
        }

    @staticmethod
    def _retention_row_ids(
        connection: sqlite3.Connection,
        *,
        table_name: str,
        timestamp_column: str,
        cutoff: str | None,
        batch_size: int,
    ) -> list[int]:
        if cutoff is None:
            return []
        rows = connection.execute(
            f"""
            SELECT id
            FROM {table_name}
            WHERE {timestamp_column} < ?
            ORDER BY {timestamp_column}, id
            LIMIT ?
            """,
            (cutoff, batch_size),
        ).fetchall()
        return [int(row[0]) for row in rows]

    @staticmethod
    def _delete_rows_by_id(
        connection: sqlite3.Connection,
        table_name: str,
        row_ids: list[int],
    ) -> int:
        if not row_ids:
            return 0
        placeholders = ", ".join("?" for _ in row_ids)
        return int(
            connection.execute(
                f"DELETE FROM {table_name} WHERE id IN ({placeholders})",
                row_ids,
            ).rowcount
        )

    @staticmethod
    def _roll_up_metric_rows(
        connection: sqlite3.Connection,
        row_ids: list[int],
        *,
        bucket_seconds: int,
    ) -> None:
        if not row_ids:
            return
        if bucket_seconds == 3600:
            bucket_expression = "strftime('%Y-%m-%dT%H:00:00+00:00', observed_at)"
        elif bucket_seconds == 86400:
            bucket_expression = "strftime('%Y-%m-%dT00:00:00+00:00', observed_at)"
        else:
            raise ValueError("Unsupported history rollup interval.")
        placeholders = ", ".join("?" for _ in row_ids)
        connection.execute(
            f"""
            INSERT INTO metric_rollups (
                bucket_start, bucket_seconds, system_id, system_label,
                enclosure_key, enclosure_id, enclosure_label, slot, slot_label,
                metric_name, sample_count, value_sum, value_min, value_max,
                last_value, last_observed_at,
                device_name, serial, model, state, gptid, persistent_id_label,
                disk_identity_key, logical_unit_id, sas_address
            )
            SELECT
                {bucket_expression}, ?, system_id, MAX(system_label),
                enclosure_key, MAX(enclosure_id), MAX(enclosure_label), slot,
                MAX(slot_label), metric_name, COUNT(*),
                SUM(COALESCE(value_real, CAST(value_integer AS REAL))),
                MIN(COALESCE(value_real, CAST(value_integer AS REAL))),
                MAX(COALESCE(value_real, CAST(value_integer AS REAL))),
                MAX(
                    CASE WHEN rollup_rank = 1
                    THEN COALESCE(value_real, CAST(value_integer AS REAL)) END
                ),
                MAX(observed_at),
                MAX(device_name), MAX(serial), MAX(model), MAX(state),
                MAX(gptid), MAX(persistent_id_label),
                COALESCE(disk_identity_key, ''), MAX(logical_unit_id), MAX(sas_address)
            FROM (
                SELECT
                    metric_samples.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            {bucket_expression}, system_id, enclosure_key, slot,
                            metric_name, COALESCE(disk_identity_key, '')
                        ORDER BY observed_at DESC, id DESC
                    ) AS rollup_rank
                FROM metric_samples
                WHERE id IN ({placeholders})
            ) selected_samples
            WHERE 1 = 1
              AND COALESCE(value_real, CAST(value_integer AS REAL)) IS NOT NULL
            GROUP BY
                {bucket_expression}, system_id, enclosure_key, slot,
                metric_name, COALESCE(disk_identity_key, '')
            ON CONFLICT (
                bucket_seconds, bucket_start, system_id, enclosure_key,
                slot, metric_name, disk_identity_key
            ) DO UPDATE SET
                system_label = COALESCE(excluded.system_label, metric_rollups.system_label),
                enclosure_id = COALESCE(excluded.enclosure_id, metric_rollups.enclosure_id),
                enclosure_label = COALESCE(excluded.enclosure_label, metric_rollups.enclosure_label),
                slot_label = excluded.slot_label,
                sample_count = metric_rollups.sample_count + excluded.sample_count,
                value_sum = metric_rollups.value_sum + excluded.value_sum,
                value_min = MIN(metric_rollups.value_min, excluded.value_min),
                value_max = MAX(metric_rollups.value_max, excluded.value_max),
                last_value = CASE
                    WHEN excluded.last_observed_at >= metric_rollups.last_observed_at
                    THEN excluded.last_value
                    ELSE metric_rollups.last_value
                END,
                last_observed_at = MAX(
                    metric_rollups.last_observed_at,
                    excluded.last_observed_at
                ),
                device_name = COALESCE(excluded.device_name, metric_rollups.device_name),
                serial = COALESCE(excluded.serial, metric_rollups.serial),
                model = COALESCE(excluded.model, metric_rollups.model),
                state = COALESCE(excluded.state, metric_rollups.state),
                gptid = COALESCE(excluded.gptid, metric_rollups.gptid),
                persistent_id_label = COALESCE(
                    excluded.persistent_id_label,
                    metric_rollups.persistent_id_label
                ),
                logical_unit_id = COALESCE(excluded.logical_unit_id, metric_rollups.logical_unit_id),
                sas_address = COALESCE(excluded.sas_address, metric_rollups.sas_address)
            """,
            [bucket_seconds, *row_ids],
        )

    @staticmethod
    def _delete_rollup_batch(
        connection: sqlite3.Connection,
        *,
        bucket_seconds: int,
        cutoff: str | None,
        batch_size: int,
    ) -> int:
        if cutoff is None:
            return 0
        return int(
            connection.execute(
                """
                DELETE FROM metric_rollups
                WHERE rowid IN (
                    SELECT rowid
                    FROM metric_rollups
                    WHERE bucket_seconds = ? AND bucket_start < ?
                    ORDER BY bucket_start
                    LIMIT ?
                )
                """,
                (bucket_seconds, cutoff, batch_size),
            ).rowcount
        )

    @staticmethod
    def _retention_rows_exist(
        connection: sqlite3.Connection,
        table_name: str,
        timestamp_column: str,
        cutoff: str | None,
    ) -> bool:
        if cutoff is None:
            return False
        row = connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM {table_name} WHERE {timestamp_column} < ? LIMIT 1)",
            (cutoff,),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def _rollups_exist(
        connection: sqlite3.Connection,
        bucket_seconds: int,
        cutoff: str | None,
    ) -> bool:
        if cutoff is None:
            return False
        row = connection.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM metric_rollups
                WHERE bucket_seconds = ? AND bucket_start < ?
                LIMIT 1
            )
            """,
            (bucket_seconds, cutoff),
        ).fetchone()
        return bool(row[0])

    def list_slot_events(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_slot_events(
                system_id,
                enclosure_id,
                slot,
                limit=limit,
            )
        enclosure_key = enclosure_id or ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM slot_events
                WHERE system_id = ? AND enclosure_key = ? AND slot = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (system_id, enclosure_key, slot, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_metric_samples(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_metric_samples(
                system_id,
                enclosure_id,
                slot,
                metric_name=metric_name,
                limit=limit,
                since=since,
            )
        enclosure_key = enclosure_id or ""
        base_where_clauses = ["system_id = ?", "enclosure_key = ?", "slot = ?"]
        base_parameters: list[Any] = [system_id, enclosure_key, slot]
        if metric_name:
            base_where_clauses.append("metric_name = ?")
            base_parameters.append(metric_name)
        where_clauses = list(base_where_clauses)
        parameters = list(base_parameters)
        if since:
            where_clauses.append("observed_at >= ?")
            parameters.append(since)
        parameters.append(limit)

        query = f"""
            SELECT *
            FROM metric_samples
            WHERE {' AND '.join(where_clauses)}
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
            samples = self._metric_rows_to_payload(rows)
            return self._append_metric_rollups(
                connection,
                samples,
                where_clauses=base_where_clauses,
                parameters=base_parameters,
                limit=limit,
                since=since,
            )

    def list_disk_metric_samples(
        self,
        disk_identity_key: str,
        *,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_disk_metric_samples(
                disk_identity_key,
                metric_name=metric_name,
                limit=limit,
                since=since,
            )
        normalized_identity_key = disk_identity_key.strip()
        if not normalized_identity_key:
            return []

        base_where_clauses = ["disk_identity_key = ?"]
        base_parameters: list[Any] = [normalized_identity_key]
        if metric_name:
            base_where_clauses.append("metric_name = ?")
            base_parameters.append(metric_name)
        where_clauses = list(base_where_clauses)
        parameters = list(base_parameters)
        if since:
            where_clauses.append("observed_at >= ?")
            parameters.append(since)
        parameters.append(limit)

        query = f"""
            SELECT *
            FROM metric_samples
            WHERE {' AND '.join(where_clauses)}
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
            samples = self._metric_rows_to_payload(rows)
            return self._append_metric_rollups(
                connection,
                samples,
                where_clauses=base_where_clauses,
                parameters=base_parameters,
                limit=limit,
                since=since,
            )

    @classmethod
    def _append_metric_rollups(
        cls,
        connection: sqlite3.Connection,
        samples: list[dict[str, Any]],
        *,
        where_clauses: list[str],
        parameters: list[Any],
        limit: int,
        since: str | None,
    ) -> list[dict[str, Any]]:
        if len(samples) >= limit:
            return samples[:limit]

        before = str(samples[-1]["observed_at"]) if samples else None
        hourly_rollup_added = False
        for bucket_seconds in (3600, 86400):
            remaining = limit - len(samples)
            if remaining <= 0:
                break
            rollup_where = [*where_clauses, "bucket_seconds = ?"]
            rollup_parameters: list[Any] = [*parameters, bucket_seconds]
            if since is not None:
                rollup_where.append("bucket_start >= ?")
                rollup_parameters.append(since)
            if before:
                boundary = (
                    f"{before[:10]}T00:00:00+00:00"
                    if bucket_seconds == 86400 and hourly_rollup_added
                    else before
                )
                rollup_where.append("bucket_start < ?")
                rollup_parameters.append(boundary)
            rollup_parameters.append(remaining)
            rows = connection.execute(
                f"""
                SELECT
{ROLLUP_TO_SAMPLE_PROJECTION}
                FROM metric_rollups
                WHERE {' AND '.join(rollup_where)}
                ORDER BY bucket_start DESC
                LIMIT ?
                """,
                rollup_parameters,
            ).fetchall()
            rollups = cls._metric_rows_to_payload(rows)
            samples.extend(rollups)
            if rollups:
                if bucket_seconds == 3600:
                    hourly_rollup_added = True
                before = str(rollups[-1]["observed_at"])
        return samples[:limit]

    def list_disk_metric_homes(
        self,
        disk_identity_key: str,
        *,
        since: str | None = None,
        limit: int = MAX_HISTORY_QUERY_LIMIT,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_QUERY_LIMIT:
            raise ValueError("History query limit is invalid.")
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_disk_metric_homes(
                disk_identity_key,
                since=since,
                limit=limit,
            )
        normalized_identity_key = disk_identity_key.strip()
        if not normalized_identity_key:
            return []
        raw_since_clause = "AND observed_at >= ?" if since else ""
        rollup_since_clause = "AND bucket_start >= ?" if since else ""
        parameters: list[Any] = [normalized_identity_key]
        if since:
            parameters.append(since)
        parameters.append(normalized_identity_key)
        if since:
            parameters.append(since)
        parameters.append(normalized_identity_key)
        if since:
            parameters.append(since)

        query = f"""
            SELECT
                system_id,
                MAX(system_label) AS system_label,
                enclosure_key,
                MAX(enclosure_id) AS enclosure_id,
                MAX(enclosure_label) AS enclosure_label,
                slot,
                MAX(slot_label) AS slot_label,
                MIN(first_seen_at) AS first_seen_at,
                MAX(last_seen_at) AS last_seen_at,
                SUM(sample_count) AS sample_count
            FROM (
                SELECT
                    system_id, system_label, enclosure_key, enclosure_id,
                    enclosure_label, slot, slot_label,
                    observed_at AS first_seen_at,
                    observed_at AS last_seen_at,
                    1 AS sample_count
                FROM metric_samples
                WHERE disk_identity_key = ? {raw_since_clause}
                UNION ALL
                SELECT
                    system_id, system_label, enclosure_key, enclosure_id,
                    enclosure_label, slot, slot_label,
                    bucket_start AS first_seen_at,
                    bucket_start AS last_seen_at,
                    sample_count
                FROM metric_rollups hourly
                WHERE disk_identity_key = ?
                  AND bucket_seconds = 3600
                  {rollup_since_clause}
                UNION ALL
                SELECT
                    system_id, system_label, enclosure_key, enclosure_id,
                    enclosure_label, slot, slot_label,
                    bucket_start AS first_seen_at,
                    bucket_start AS last_seen_at,
                    sample_count
                FROM metric_rollups daily
                WHERE disk_identity_key = ?
                  AND bucket_seconds = 86400
                  {rollup_since_clause}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM metric_rollups hourly
                      WHERE hourly.disk_identity_key = daily.disk_identity_key
                        AND hourly.system_id = daily.system_id
                        AND hourly.enclosure_key = daily.enclosure_key
                        AND hourly.slot = daily.slot
                        AND hourly.bucket_seconds = 3600
                        AND substr(hourly.bucket_start, 1, 10) = substr(daily.bucket_start, 1, 10)
                  )
            ) history
            GROUP BY system_id, enclosure_key, slot
            ORDER BY first_seen_at ASC, last_seen_at ASC, system_id ASC, enclosure_key ASC, slot ASC
            LIMIT ?
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(query, [*parameters, limit]).fetchall()
        return [dict(row) for row in rows]

    def list_followed_metric_samples(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        disk_identity_key: str,
        *,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_followed_metric_samples(
                system_id,
                enclosure_id,
                slot,
                disk_identity_key,
                metric_name=metric_name,
                limit=limit,
                since=since,
            )
        disk_samples = self.list_disk_metric_samples(
            disk_identity_key,
            metric_name=metric_name,
            limit=limit,
            since=since,
        )
        local_samples = self.list_metric_samples(
            system_id,
            enclosure_id,
            slot,
            metric_name=metric_name,
            limit=limit,
            since=since,
        )
        merged_by_key: dict[Any, dict[str, Any]] = {}
        for item in [*disk_samples, *local_samples]:
            item_id = item.get("id")
            if item_id is not None:
                key: Any = ("id", item_id)
            else:
                key = (
                    item.get("observed_at"),
                    item.get("metric_name"),
                    item.get("system_id"),
                    item.get("enclosure_key"),
                    item.get("slot"),
                    item.get("value"),
                )
            merged_by_key[key] = item

        return sorted(
            merged_by_key.values(),
            key=lambda item: (str(item.get("observed_at") or ""), int(item.get("id") or 0)),
            reverse=True,
        )[:limit]

    def get_slot_history_bundle(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        *,
        event_limit: int = 12,
        metric_limits: dict[str, int] | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.get_slot_history_bundle(
                system_id,
                enclosure_id,
                slot,
                event_limit=event_limit,
                metric_limits=metric_limits,
                since=since,
            )
        current = self.get_slot_state(system_id, enclosure_id, slot)
        events = self.list_slot_events(system_id, enclosure_id, slot, limit=event_limit)
        metric_limits = metric_limits or {}

        metrics: dict[str, list[dict[str, Any]]] = {}
        latest_values: dict[str, Any] = {}
        sample_counts: dict[str, int] = {}
        disk_history: dict[str, Any] = {
            "identity_available": False,
            "followed": False,
            "serial": current.serial if current else None,
            "persistent_id_label": current.persistent_id_label if current else None,
            "persistent_id": current.gptid if current else None,
            "current_home": None,
            "homes": [],
            "prior_home_count": 0,
            "window_limited": bool(since),
        }

        current_enclosure_key = enclosure_id or ""
        current_home_key = (system_id, current_enclosure_key, slot)

        for metric_name, limit in metric_limits.items():
            if current and current.disk_identity_key:
                samples = self.list_followed_metric_samples(
                    system_id,
                    enclosure_id,
                    slot,
                    current.disk_identity_key,
                    metric_name=metric_name,
                    limit=limit,
                    since=since,
                )
            else:
                samples = self.list_metric_samples(
                    system_id,
                    enclosure_id,
                    slot,
                    metric_name=metric_name,
                    limit=limit,
                    since=since,
                )
            metrics[metric_name] = samples
            latest_values[metric_name] = samples[0].get("value") if samples else None
            sample_counts[metric_name] = len(samples)

        if current and current.disk_identity_key:
            homes = self.list_disk_metric_homes(current.disk_identity_key, since=since)
            disk_history["identity_available"] = True
            disk_history["homes"] = homes
            def home_scope_key(home: dict[str, Any]) -> tuple[str | None, str, int]:
                slot_value = home.get("slot")
                normalized_slot = int(slot_value) if slot_value is not None else -1
                return (home.get("system_id"), home.get("enclosure_key") or "", normalized_slot)

            disk_history["current_home"] = next(
                (
                    home
                    for home in homes
                    if home_scope_key(home) == current_home_key
                ),
                None,
            )
            disk_history["prior_home_count"] = sum(
                1
                for home in homes
                if home_scope_key(home) != current_home_key
            )
            disk_history["followed"] = bool(disk_history["prior_home_count"])

        return {
            "events": events,
            "metrics": metrics,
            "sample_counts": sample_counts,
            "latest_values": latest_values,
            "disk_history": disk_history,
        }

    @staticmethod
    def _metric_rows_to_payload(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["value"] = item["value_integer"] if item["value_integer"] is not None else item["value_real"]
            payload.append(item)
        return payload

    @classmethod
    def _append_scope_metric_rollups(
        cls,
        connection: sqlite3.Connection,
        payload_by_slot: dict[int, dict[str, Any]],
        *,
        where_clauses: list[str],
        parameters: list[Any],
        metric_name: str,
        limit: int,
        since: str | None,
    ) -> None:
        if limit <= 0:
            return
        rollups_by_interval: dict[int, dict[int, list[dict[str, Any]]]] = {}
        for bucket_seconds in (3600, 86400):
            rollup_where = [
                *where_clauses,
                "metric_name = ?",
                "bucket_seconds = ?",
            ]
            rollup_parameters = [*parameters, metric_name, bucket_seconds]
            if since is not None:
                rollup_where.append("bucket_start >= ?")
                rollup_parameters.append(since)
            rollup_parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT *
                FROM (
                    SELECT
{_indent_sql(ROLLUP_TO_SAMPLE_PROJECTION, 4)},
                        ROW_NUMBER() OVER (
                            PARTITION BY slot, metric_name
                            ORDER BY bucket_start DESC
                        ) AS row_number
                    FROM metric_rollups
                    WHERE {' AND '.join(rollup_where)}
                )
                WHERE row_number <= ?
                ORDER BY slot, observed_at DESC
                """,
                rollup_parameters,
            ).fetchall()
            by_slot: dict[int, list[dict[str, Any]]] = {}
            for item in cls._metric_rows_to_payload(rows):
                item.pop("row_number", None)
                by_slot.setdefault(int(item["slot"]), []).append(item)
            rollups_by_interval[bucket_seconds] = by_slot

        for slot, payload in payload_by_slot.items():
            samples = payload.setdefault("metrics", {}).setdefault(metric_name, [])
            before = str(samples[-1]["observed_at"]) if samples else None
            hourly_rollup_added = False
            for bucket_seconds in (3600, 86400):
                if len(samples) >= limit:
                    break
                boundary = None
                if before:
                    boundary = (
                        f"{before[:10]}T00:00:00+00:00"
                        if bucket_seconds == 86400 and hourly_rollup_added
                        else before
                    )
                candidates = rollups_by_interval[bucket_seconds].get(slot, [])
                additions = [
                    item
                    for item in candidates
                    if boundary is None or str(item["observed_at"]) < boundary
                ][: max(0, limit - len(samples))]
                samples.extend(additions)
                if additions:
                    if bucket_seconds == 3600:
                        hourly_rollup_added = True
                    before = str(additions[-1]["observed_at"])

    @staticmethod
    def _empty_slot_history_payload(metric_names: Iterable[str]) -> dict[str, Any]:
        return {
            "events": [],
            "metrics": {metric_name: [] for metric_name in metric_names},
            "sample_counts": {},
            "latest_values": {},
        }

    def list_scope_history(
        self,
        system_id: str,
        enclosure_id: str | None,
        *,
        slots: list[int] | None = None,
        event_limit: int = 12,
        metric_limits: dict[str, int] | None = None,
        since: str | None = None,
    ) -> dict[int, dict[str, Any]]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_scope_history(
                system_id,
                enclosure_id,
                slots=slots,
                event_limit=event_limit,
                metric_limits=metric_limits,
                since=since,
            )
        enclosure_key = enclosure_id or ""
        slot_numbers = sorted({int(slot) for slot in (slots or [])})
        metric_limits = metric_limits or {}
        payload_by_slot: dict[int, dict[str, Any]] = {
            slot: self._empty_slot_history_payload(metric_limits)
            for slot in slot_numbers
        }

        where_clauses = ["system_id = ?", "enclosure_key = ?"]
        parameters: list[Any] = [system_id, enclosure_key]
        if slot_numbers:
            placeholders = ", ".join("?" for _ in slot_numbers)
            where_clauses.append(f"slot IN ({placeholders})")
            parameters.extend(slot_numbers)
        scope_where = " AND ".join(where_clauses)

        with closing(self._connect()) as connection:
            slot_rows = connection.execute(
                f"""
                SELECT slot
                FROM slot_state_current
                WHERE {scope_where}
                ORDER BY slot
                """,
                parameters,
            ).fetchall()
            for row in slot_rows:
                slot = int(row["slot"])
                payload_by_slot.setdefault(
                    slot,
                    self._empty_slot_history_payload(metric_limits),
                )

            if event_limit > 0:
                event_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM (
                        SELECT
                            *,
                            ROW_NUMBER() OVER (
                                PARTITION BY slot
                                ORDER BY observed_at DESC, id DESC
                            ) AS row_number
                        FROM slot_events
                        WHERE {scope_where}
                    )
                    WHERE row_number <= ?
                    ORDER BY slot, observed_at DESC, id DESC
                    """,
                    [*parameters, event_limit],
                ).fetchall()
                for row in event_rows:
                    item = dict(row)
                    slot = int(item["slot"])
                    item.pop("row_number", None)
                    payload_by_slot.setdefault(
                        slot,
                        self._empty_slot_history_payload(metric_limits),
                    )["events"].append(item)

            for metric_name, limit in metric_limits.items():
                metric_where_clauses = [*where_clauses, "metric_name = ?"]
                metric_parameters = [*parameters, metric_name]
                if since:
                    metric_where_clauses.append("observed_at >= ?")
                    metric_parameters.append(since)
                metric_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM (
                        SELECT
                            *,
                            ROW_NUMBER() OVER (
                                PARTITION BY slot, metric_name
                            ORDER BY observed_at DESC, id DESC
                        ) AS row_number
                        FROM metric_samples
                        WHERE {' AND '.join(metric_where_clauses)}
                    )
                    WHERE row_number <= ?
                    ORDER BY slot, observed_at DESC, id DESC
                    """,
                    [*metric_parameters, limit],
                ).fetchall()
                for row in metric_rows:
                    item = dict(row)
                    slot = int(item["slot"])
                    item["value"] = item["value_integer"] if item["value_integer"] is not None else item["value_real"]
                    item.pop("row_number", None)
                    payload_by_slot.setdefault(
                        slot,
                        self._empty_slot_history_payload(metric_limits),
                    )["metrics"].setdefault(metric_name, []).append(item)
                self._append_scope_metric_rollups(
                    connection,
                    payload_by_slot,
                    where_clauses=where_clauses,
                    parameters=parameters,
                    metric_name=metric_name,
                    limit=limit,
                    since=since,
                )

        for slot, payload in payload_by_slot.items():
            metrics = payload.setdefault("metrics", {})
            for metric_name in metric_limits:
                samples = metrics.setdefault(metric_name, [])
                payload["sample_counts"][metric_name] = len(samples)
                payload["latest_values"][metric_name] = samples[0]["value"] if samples else None

        return payload_by_slot

    def list_scopes(self, *, include_activity_counts: bool = True) -> list[dict[str, Any]]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_scopes(
                include_activity_counts=include_activity_counts,
            )
        with closing(self._connect()) as connection:
            if not include_activity_counts:
                rows = connection.execute(
                    """
                    SELECT
                        current.system_id,
                        current.system_label,
                        current.enclosure_id,
                        current.enclosure_label,
                        current.enclosure_key,
                        COUNT(*) AS tracked_slots,
                        MAX(current.last_seen_at) AS last_seen_at,
                        NULL AS event_count,
                        NULL AS metric_sample_count,
                        NULL AS metric_rollup_count,
                        1 AS activity_counts_deferred
                    FROM slot_state_current current
                    GROUP BY
                        current.system_id,
                        current.system_label,
                        current.enclosure_id,
                        current.enclosure_label,
                        current.enclosure_key
                    ORDER BY current.system_label, current.enclosure_label
                    """
                ).fetchall()
                return [dict(row) for row in rows]

            rows = connection.execute(
                """
                SELECT
                    current.system_id,
                    current.system_label,
                    current.enclosure_id,
                    current.enclosure_label,
                    current.enclosure_key,
                    COUNT(*) AS tracked_slots,
                    MAX(current.last_seen_at) AS last_seen_at,
                    (
                        SELECT COUNT(*)
                        FROM slot_events events
                        WHERE events.system_id = current.system_id
                          AND events.enclosure_key = current.enclosure_key
                    ) AS event_count,
                    (
                        SELECT COUNT(*)
                        FROM metric_samples metrics
                        WHERE metrics.system_id = current.system_id
                          AND metrics.enclosure_key = current.enclosure_key
                    ) AS metric_sample_count,
                    (
                        SELECT COUNT(*)
                        FROM metric_rollups rollups
                        WHERE rollups.system_id = current.system_id
                          AND rollups.enclosure_key = current.enclosure_key
                    ) AS metric_rollup_count
                FROM slot_state_current current
                GROUP BY
                    current.system_id,
                    current.system_label,
                    current.enclosure_id,
                    current.enclosure_label,
                    current.enclosure_key
                ORDER BY current.system_label, current.enclosure_label
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.counts()
        with closing(self._connect()) as connection:
            tracked_slots = int(connection.execute("SELECT COUNT(*) FROM slot_state_current").fetchone()[0])
            event_count = int(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0])
            metric_sample_count = int(connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0])
            metric_rollup_count = int(connection.execute("SELECT COUNT(*) FROM metric_rollups").fetchone()[0])
        return {
            "tracked_slots": tracked_slots,
            "event_count": event_count,
            "metric_sample_count": metric_sample_count,
            "metric_rollup_count": metric_rollup_count,
        }

    def estimated_counts(self) -> dict[str, Any]:
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return {
                **segmented_reader.counts(),
                "estimated": False,
                "count_mode": "segmented-exact",
            }
        with closing(self._connect()) as connection:
            tracked_slots = int(connection.execute("SELECT COUNT(*) FROM slot_state_current").fetchone()[0])
            tracked_counts = {
                str(row["table_name"]): int(row["row_count"])
                for row in connection.execute(
                    "SELECT table_name, row_count FROM history_table_counts"
                ).fetchall()
            }
        return {
            "tracked_slots": tracked_slots,
            "event_count": tracked_counts.get("slot_events", 0),
            "metric_sample_count": tracked_counts.get("metric_samples", 0),
            "metric_rollup_count": tracked_counts.get("metric_rollups", 0),
            "estimated": False,
            "count_mode": "tracked",
        }

    def list_history_system_summaries(
        self,
        exclude_system_ids: list[str] | tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        normalized_excludes = tuple(
            sorted({system_id.strip() for system_id in exclude_system_ids if system_id and system_id.strip()})
        )
        segmented_reader = self._segmented_reader()
        if segmented_reader is not None:
            return segmented_reader.list_history_system_summaries(normalized_excludes)
        if not self.file_path.exists():
            return []
        with closing(self._connect()) as connection:
            return self._list_history_system_summaries(connection, exclude_system_ids=normalized_excludes)

    def delete_system_history(self, system_id: str) -> dict[str, Any]:
        self._require_unsegmented_operation("system deletion")
        normalized_system_id = system_id.strip()
        if not normalized_system_id:
            return self._empty_cleanup_summary()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            summary = self._delete_history_for_system_ids(connection, [normalized_system_id])
            summary["removed_system_ids"] = [normalized_system_id] if summary["total_rows"] else []
            return summary

        return self._execute_write(operation)

    def purge_orphaned_history(self, valid_system_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        self._require_unsegmented_operation("orphan purge")
        normalized_valid_ids = tuple(
            sorted({system_id.strip() for system_id in valid_system_ids if system_id and system_id.strip()})
        )

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            orphan_ids = self._list_cleanup_system_ids(connection, exclude_system_ids=normalized_valid_ids)
            if not orphan_ids:
                return self._empty_cleanup_summary()
            summary = self._delete_history_for_system_ids(connection, orphan_ids)
            summary["removed_system_ids"] = orphan_ids
            return summary

        return self._execute_write(operation)

    def adopt_system_history(
        self,
        source_system_id: str,
        target_system_id: str,
        *,
        target_system_label: str | None = None,
    ) -> dict[str, Any]:
        self._require_unsegmented_operation("system adoption")
        normalized_source_id = source_system_id.strip()
        normalized_target_id = target_system_id.strip()
        normalized_target_label = target_system_label.strip() if target_system_label and target_system_label.strip() else None
        if not normalized_source_id:
            raise ValueError("Source system id is required.")
        if not normalized_target_id:
            raise ValueError("Target system id is required.")
        if normalized_source_id == normalized_target_id:
            raise ValueError("Source and target system ids must be different.")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            source_slot_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM slot_state_current WHERE system_id = ?",
                    (normalized_source_id,),
                ).fetchone()[0]
            )
            inserted_slot_count = int(
                connection.execute(
                    SLOT_STATE_ADOPTION_SQL,
                    (
                        normalized_target_id,
                        normalized_target_label,
                        normalized_source_id,
                    ),
                ).rowcount
            )
            connection.execute(
                "DELETE FROM slot_state_current WHERE system_id = ?",
                (normalized_source_id,),
            )
            event_count = int(
                connection.execute(
                    "UPDATE slot_events SET system_id = ?, system_label = ? WHERE system_id = ?",
                    (
                        normalized_target_id,
                        normalized_target_label,
                        normalized_source_id,
                    ),
                ).rowcount
            )
            metric_sample_count = int(
                connection.execute(
                    "UPDATE metric_samples SET system_id = ?, system_label = ? WHERE system_id = ?",
                    (
                        normalized_target_id,
                        normalized_target_label,
                        normalized_source_id,
                    ),
                ).rowcount
            )
            metric_rollup_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM metric_rollups WHERE system_id = ?",
                    (normalized_source_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO metric_rollups (
                    bucket_start, bucket_seconds, system_id, system_label,
                    enclosure_key, enclosure_id, enclosure_label, slot, slot_label,
                    metric_name, sample_count, value_sum, value_min, value_max,
                    last_value, last_observed_at,
                    device_name, serial, model, state, gptid, persistent_id_label,
                    disk_identity_key, logical_unit_id, sas_address
                )
                SELECT
                    bucket_start, bucket_seconds, ?, ?, enclosure_key,
                    enclosure_id, enclosure_label, slot, slot_label, metric_name,
                    sample_count, value_sum, value_min, value_max,
                    last_value, last_observed_at, device_name,
                    serial, model, state, gptid, persistent_id_label,
                    disk_identity_key, logical_unit_id, sas_address
                FROM metric_rollups
                WHERE system_id = ?
                ON CONFLICT (
                    bucket_seconds, bucket_start, system_id, enclosure_key,
                    slot, metric_name, disk_identity_key
                ) DO UPDATE SET
                    system_label = excluded.system_label,
                    sample_count = metric_rollups.sample_count + excluded.sample_count,
                    value_sum = metric_rollups.value_sum + excluded.value_sum,
                    value_min = MIN(metric_rollups.value_min, excluded.value_min),
                    value_max = MAX(metric_rollups.value_max, excluded.value_max),
                    last_value = CASE
                        WHEN excluded.last_observed_at >= metric_rollups.last_observed_at
                        THEN excluded.last_value
                        ELSE metric_rollups.last_value
                    END,
                    last_observed_at = MAX(
                        metric_rollups.last_observed_at,
                        excluded.last_observed_at
                    )
                """,
                (
                    normalized_target_id,
                    normalized_target_label,
                    normalized_source_id,
                ),
            )
            connection.execute(
                "DELETE FROM metric_rollups WHERE system_id = ?",
                (normalized_source_id,),
            )
            return {
                "source_system_id": normalized_source_id,
                "target_system_id": normalized_target_id,
                "target_system_label": normalized_target_label,
                "tracked_slots": source_slot_count,
                "event_count": event_count,
                "metric_sample_count": metric_sample_count,
                "metric_rollup_count": metric_rollup_count,
                "total_rows": source_slot_count + event_count + metric_sample_count + metric_rollup_count,
                "slot_state_conflicts": max(source_slot_count - max(inserted_slot_count, 0), 0),
            }

        return self._execute_write(operation)

    def _execute_write(self, operation: Any, *, migration_lock_held: bool = False) -> Any:
        lock_context = (
            nullcontext()
            if migration_lock_held
            else history_write_lock(self.file_path, blocking=False)
        )
        with lock_context:
            with self._lock:
                self._require_no_pending_lifecycle_markers()
                readonly_repair_attempted = False
                lock_retry_count = 0
                while True:
                    try:
                        with closing(self._connect(migration_lock_held=True)) as connection:
                            result = operation(connection)
                            connection.commit()
                            return result
                    except sqlite3.OperationalError as exc:
                        if not readonly_repair_attempted and self._is_readonly_database_error(exc):
                            readonly_repair_attempted = True
                            if self._attempt_readonly_database_repair(exc):
                                continue
                        if self._is_database_locked_error(exc) and lock_retry_count < SQLITE_WRITE_LOCK_RETRY_ATTEMPTS:
                            lock_retry_count += 1
                            time.sleep(SQLITE_WRITE_LOCK_RETRY_DELAY_SECONDS * lock_retry_count)
                            continue
                        raise

    def _attempt_readonly_database_repair(self, exc: sqlite3.OperationalError) -> bool:
        if not self.permission_repair_enabled:
            logger.warning(
                "History database %s is readonly and automatic permission repair is disabled; "
                "fix host ownership/modes or explicitly enable bounded repair. Error: %s",
                self.file_path,
                exc,
            )
            return False
        logger.warning(
            "History database %s became readonly; attempting local permission repair before retrying. Error: %s",
            self.file_path,
            exc,
        )
        try:
            self._normalize_database_permissions()
        except OSError as repair_exc:
            logger.warning(
                "History database %s permission repair failed: %s",
                self.file_path,
                repair_exc,
            )
            return False
        return True

    def _normalize_database_permissions(self) -> None:
        if not self.permission_repair_enabled:
            return
        self._normalize_shared_path_permissions(self.file_path.parent, is_dir=True)
        self._normalize_shared_path_permissions(self.file_path)
        for suffix in ("-shm", "-wal"):
            self._normalize_shared_path_permissions(Path(f"{self.file_path}{suffix}"))

    def _normalize_shared_path_permissions(self, path: Path, *, is_dir: bool | None = None) -> None:
        if not self.permission_repair_enabled:
            return
        self._set_shared_path_permissions(path, is_dir=is_dir)

    def _set_shared_path_permissions(self, path: Path, *, is_dir: bool | None = None) -> None:
        try:
            initial_metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(initial_metadata.st_mode):
            raise ValueError(f"History permission repair refuses symlink path {path}.")
        if is_dir is None:
            is_dir = stat.S_ISDIR(initial_metadata.st_mode)
        expected_type = stat.S_ISDIR if is_dir else stat.S_ISREG
        if not expected_type(initial_metadata.st_mode):
            raise ValueError(f"History permission repair refuses non-regular path {path}.")

        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        if is_dir:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not expected_type(opened_metadata.st_mode):
                raise ValueError(f"History permission repair refuses non-regular path {path}.")
            if (opened_metadata.st_dev, opened_metadata.st_ino) != (
                initial_metadata.st_dev,
                initial_metadata.st_ino,
            ):
                raise ValueError(f"History permission repair refuses changed path {path}.")

            current_mode = stat.S_IMODE(opened_metadata.st_mode)
            target_mode = self.shared_dir_mode if is_dir else self.shared_file_mode
            if target_mode != current_mode:
                os.fchmod(descriptor, target_mode)
        finally:
            os.close(descriptor)

    def _publish_replacement(
        self,
        temp_path: Path,
        target_path: Path,
        *,
        temp_descriptor: int | None = None,
    ) -> None:
        with ExitStack() as descriptor_stack:
            if temp_descriptor is None:
                temp_descriptor, temp_metadata = self._open_stable_regular_file(
                    temp_path,
                    role="temporary",
                )
                descriptor_stack.callback(os.close, temp_descriptor)
            else:
                descriptor_stack.callback(os.close, temp_descriptor)
                temp_metadata = os.fstat(temp_descriptor)
                if not stat.S_ISREG(temp_metadata.st_mode):
                    raise ValueError(f"History replacement refuses non-regular temporary path {temp_path}.")
            if not self._path_matches_metadata(temp_path, temp_metadata):
                raise ValueError(f"History replacement refuses changed temporary path {temp_path}.")

            try:
                initial_target_metadata = target_path.lstat()
            except FileNotFoundError:
                initial_target_metadata = None

            target_descriptor: int | None = None
            target_metadata: os.stat_result | None = None
            if initial_target_metadata is not None:
                if not stat.S_ISREG(initial_target_metadata.st_mode):
                    raise ValueError(f"History replacement refuses non-regular target path {target_path}.")
                try:
                    target_descriptor, target_metadata = self._open_stable_regular_file(
                        target_path,
                        role="target",
                    )
                except FileNotFoundError as exc:
                    raise ValueError(f"History replacement refuses changed target path {target_path}.") from exc
                descriptor_stack.callback(os.close, target_descriptor)
                if (target_metadata.st_dev, target_metadata.st_ino) != (
                    initial_target_metadata.st_dev,
                    initial_target_metadata.st_ino,
                ):
                    raise ValueError(f"History replacement refuses changed target path {target_path}.")

            target_mode = self.shared_file_mode if self.permission_repair_enabled else None
            if target_metadata is not None and not self.permission_repair_enabled:
                target_mode = stat.S_IMODE(target_metadata.st_mode)
            if target_mode is not None and stat.S_IMODE(temp_metadata.st_mode) != target_mode:
                os.fchmod(temp_descriptor, target_mode)
                temp_metadata = os.fstat(temp_descriptor)
            if not self._path_matches_metadata(temp_path, temp_metadata):
                raise ValueError(f"History replacement refuses changed temporary path {temp_path}.")

            if target_metadata is None:
                self._publish_absent_target(temp_path, target_path, temp_metadata, target_mode)
                return
            self._exchange_existing_target(
                temp_path,
                target_path,
                temp_metadata,
                target_metadata,
                target_mode,
            )

    @staticmethod
    def _open_stable_regular_file(path: Path, *, role: str) -> tuple[int, os.stat_result]:
        initial_metadata = path.lstat()
        if not stat.S_ISREG(initial_metadata.st_mode):
            raise ValueError(f"History replacement refuses non-regular {role} path {path}.")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise ValueError(f"History replacement refuses non-regular {role} path {path}.")
            if (opened_metadata.st_dev, opened_metadata.st_ino) != (
                initial_metadata.st_dev,
                initial_metadata.st_ino,
            ):
                raise ValueError(f"History replacement refuses changed {role} path {path}.")
            return descriptor, opened_metadata
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _rename_at2(source_path: Path, target_path: Path, *, flags: int) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            AT_FDCWD,
            os.fsencode(source_path),
            AT_FDCWD,
            os.fsencode(target_path),
            flags,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), source_path, target_path)

    def _publish_absent_target(
        self,
        temp_path: Path,
        target_path: Path,
        temp_metadata: os.stat_result,
        target_mode: int | None,
    ) -> None:
        try:
            self._rename_at2(temp_path, target_path, flags=RENAME_NOREPLACE)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                self._discard_owned_path(temp_path, temp_metadata)
                raise ValueError(f"History replacement refuses changed target path {target_path}.") from exc
            raise
        try:
            published_metadata = target_path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"History replacement refuses changed published path {target_path}.") from exc
        if not self._metadata_identity_matches(published_metadata, temp_metadata):
            raise ValueError(f"History replacement refuses changed published path {target_path}.")
        if target_mode is not None and stat.S_IMODE(published_metadata.st_mode) != target_mode:
            self._unlink_owned_path(target_path, temp_metadata)
            raise ValueError(f"History replacement refuses changed temporary mode for {temp_path}.")

    def _exchange_existing_target(
        self,
        temp_path: Path,
        target_path: Path,
        temp_metadata: os.stat_result,
        target_metadata: os.stat_result,
        target_mode: int | None,
    ) -> None:
        self._rename_at2(temp_path, target_path, flags=RENAME_EXCHANGE)
        try:
            published_metadata = target_path.lstat()
            displaced_metadata = temp_path.lstat()
            if not self._metadata_identity_matches(published_metadata, temp_metadata):
                raise ValueError(f"History replacement refuses changed temporary path {temp_path}.")
            if not self._metadata_identity_matches(displaced_metadata, target_metadata):
                raise ValueError(f"History replacement refuses changed target path {target_path}.")
            if target_mode is not None and stat.S_IMODE(published_metadata.st_mode) != target_mode:
                raise ValueError(f"History replacement refuses changed temporary mode for {temp_path}.")
            self._unlink_owned_path(temp_path, target_metadata)
        except Exception:
            self._rollback_exchange(
                temp_path,
                target_path,
                temp_metadata,
                target_metadata,
            )
            raise

    def _rollback_exchange(
        self,
        temp_path: Path,
        target_path: Path,
        temp_metadata: os.stat_result,
        target_metadata: os.stat_result,
    ) -> None:
        target_is_published_temp = self._path_matches_metadata(target_path, temp_metadata)
        temp_is_displaced_target = self._path_matches_metadata(temp_path, target_metadata)
        if not target_is_published_temp or not temp_is_displaced_target:
            return
        self._rename_at2(target_path, temp_path, flags=RENAME_EXCHANGE)
        self._discard_owned_path(temp_path, temp_metadata)

    @staticmethod
    def _metadata_identity_matches(current: os.stat_result, expected: os.stat_result) -> bool:
        return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        )

    @classmethod
    def _path_matches_metadata(cls, path: Path, expected: os.stat_result) -> bool:
        try:
            current = path.lstat()
        except FileNotFoundError:
            return False
        return cls._metadata_identity_matches(current, expected)

    @classmethod
    def _unlink_owned_path(cls, path: Path, expected: os.stat_result) -> None:
        parent_descriptor = cls._open_private_cleanup_directory(path.parent)
        try:
            try:
                current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not cls._metadata_identity_matches(current, expected):
                raise ValueError(f"History replacement refuses changed cleanup path {path}.")
            os.unlink(path.name, dir_fd=parent_descriptor)
        finally:
            os.close(parent_descriptor)

    @classmethod
    def _discard_owned_path(cls, path: Path, expected: os.stat_result) -> None:
        parent_descriptor = cls._open_private_cleanup_directory(path.parent)
        try:
            try:
                current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            if cls._metadata_identity_matches(current, expected):
                os.unlink(path.name, dir_fd=parent_descriptor)
        finally:
            os.close(parent_descriptor)

    @staticmethod
    def _create_private_replacement_file(
        parent: Path,
        *,
        prefix: str,
        suffix: str,
    ) -> tuple[int, Path]:
        replacement_root = parent / f"{PRIVATE_REPLACEMENT_DIR_PREFIX}{os.geteuid()}"
        try:
            replacement_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        directory_descriptor = HistoryStore._open_private_cleanup_directory(replacement_root)
        try:
            for _ in range(32):
                name = f"{prefix}{secrets.token_hex(8)}{suffix}"
                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
                except FileExistsError:
                    continue
                return descriptor, replacement_root / name
            raise FileExistsError("Unable to allocate a private history replacement path.")
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _open_private_cleanup_directory(path: Path) -> int:
        initial_metadata = path.lstat()
        if not stat.S_ISDIR(initial_metadata.st_mode) or stat.S_IMODE(initial_metadata.st_mode) != 0o700:
            raise ValueError(f"History replacement refuses non-private cleanup directory {path}.")
        if initial_metadata.st_uid != os.geteuid():
            raise ValueError(f"History replacement refuses foreign cleanup directory {path}.")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if (opened_metadata.st_dev, opened_metadata.st_ino) != (
                initial_metadata.st_dev,
                initial_metadata.st_ino,
            ):
                raise ValueError(f"History replacement refuses changed cleanup directory {path}.")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _copy_file_to_descriptor(source_path: Path, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        with source_path.open("rb") as source, os.fdopen(os.dup(descriptor), "wb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())

    def _preserve_existing_target_mode(self, temp_path: Path, target_path: Path) -> None:
        if self.permission_repair_enabled:
            return
        target_mode = self._stable_regular_file_mode(target_path)
        if target_mode is None:
            return

        initial_metadata = temp_path.lstat()
        if not stat.S_ISREG(initial_metadata.st_mode):
            raise ValueError(f"History replacement refuses non-regular temporary path {temp_path}.")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(temp_path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise ValueError(f"History replacement refuses non-regular temporary path {temp_path}.")
            if (opened_metadata.st_dev, opened_metadata.st_ino) != (
                initial_metadata.st_dev,
                initial_metadata.st_ino,
            ):
                raise ValueError(f"History replacement refuses changed temporary path {temp_path}.")
            if stat.S_IMODE(opened_metadata.st_mode) != target_mode:
                os.fchmod(descriptor, target_mode)
        finally:
            os.close(descriptor)

    @staticmethod
    def _stable_regular_file_mode(path: Path) -> int | None:
        try:
            initial_metadata = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(initial_metadata.st_mode):
            raise ValueError(f"History replacement refuses non-regular target path {path}.")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise ValueError(f"History replacement refuses non-regular target path {path}.")
            if (opened_metadata.st_dev, opened_metadata.st_ino) != (
                initial_metadata.st_dev,
                initial_metadata.st_ino,
            ):
                raise ValueError(f"History replacement refuses changed target path {path}.")
            return stat.S_IMODE(opened_metadata.st_mode)
        finally:
            os.close(descriptor)

    @staticmethod
    def _empty_cleanup_summary() -> dict[str, Any]:
        return {
            "tracked_slots": 0,
            "event_count": 0,
            "metric_sample_count": 0,
            "metric_rollup_count": 0,
            "total_rows": 0,
            "removed_system_ids": [],
        }

    @staticmethod
    def _delete_history_for_system_ids(connection: sqlite3.Connection, system_ids: list[str]) -> dict[str, Any]:
        if not system_ids:
            return HistoryStore._empty_cleanup_summary()

        placeholders = ", ".join("?" for _ in system_ids)
        tracked_slots = int(
            connection.execute(
                f"DELETE FROM slot_state_current WHERE system_id IN ({placeholders})",
                system_ids,
            ).rowcount
        )
        event_count = int(
            connection.execute(
                f"DELETE FROM slot_events WHERE system_id IN ({placeholders})",
                system_ids,
            ).rowcount
        )
        metric_sample_count = int(
            connection.execute(
                f"DELETE FROM metric_samples WHERE system_id IN ({placeholders})",
                system_ids,
            ).rowcount
        )
        metric_rollup_count = int(
            connection.execute(
                f"DELETE FROM metric_rollups WHERE system_id IN ({placeholders})",
                system_ids,
            ).rowcount
        )
        return {
            "tracked_slots": tracked_slots,
            "event_count": event_count,
            "metric_sample_count": metric_sample_count,
            "metric_rollup_count": metric_rollup_count,
            "total_rows": tracked_slots + event_count + metric_sample_count + metric_rollup_count,
            "removed_system_ids": [],
        }

    @staticmethod
    def _list_history_system_summaries(
        connection: sqlite3.Connection,
        *,
        exclude_system_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        parameters: list[str] = []
        query = """
            SELECT
                system_id,
                MAX(system_label) AS system_label,
                SUM(tracked_slots) AS tracked_slots,
                SUM(event_count) AS event_count,
                SUM(metric_sample_count) AS metric_sample_count,
                SUM(metric_rollup_count) AS metric_rollup_count
            FROM (
                SELECT
                    system_id,
                    MAX(system_label) AS system_label,
                    COUNT(*) AS tracked_slots,
                    0 AS event_count,
                    0 AS metric_sample_count,
                    0 AS metric_rollup_count
                FROM slot_state_current
                GROUP BY system_id
                UNION ALL
                SELECT
                    system_id,
                    MAX(system_label) AS system_label,
                    0 AS tracked_slots,
                    COUNT(*) AS event_count,
                    0 AS metric_sample_count,
                    0 AS metric_rollup_count
                FROM slot_events
                GROUP BY system_id
                UNION ALL
                SELECT
                    system_id,
                    MAX(system_label) AS system_label,
                    0 AS tracked_slots,
                    0 AS event_count,
                    COUNT(*) AS metric_sample_count,
                    0 AS metric_rollup_count
                FROM metric_samples
                GROUP BY system_id
                UNION ALL
                SELECT
                    system_id,
                    MAX(system_label) AS system_label,
                    0 AS tracked_slots,
                    0 AS event_count,
                    0 AS metric_sample_count,
                    COUNT(*) AS metric_rollup_count
                FROM metric_rollups
                GROUP BY system_id
            )
        """
        if exclude_system_ids:
            placeholders = ", ".join("?" for _ in exclude_system_ids)
            query += f" WHERE system_id NOT IN ({placeholders})"
            parameters.extend(exclude_system_ids)
        query += """
            GROUP BY system_id
            ORDER BY system_id
        """
        rows = connection.execute(query, parameters).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            system_id = str(row["system_id"] or "").strip()
            if not system_id:
                continue
            tracked_slots = int(row["tracked_slots"] or 0)
            event_count = int(row["event_count"] or 0)
            metric_sample_count = int(row["metric_sample_count"] or 0)
            metric_rollup_count = int(row["metric_rollup_count"] or 0)
            summaries.append(
                {
                    "system_id": system_id,
                    "system_label": row["system_label"],
                    "tracked_slots": tracked_slots,
                    "event_count": event_count,
                    "metric_sample_count": metric_sample_count,
                    "metric_rollup_count": metric_rollup_count,
                    "total_rows": tracked_slots + event_count + metric_sample_count + metric_rollup_count,
                }
            )
        return summaries

    @staticmethod
    def _list_cleanup_system_ids(
        connection: sqlite3.Connection,
        *,
        exclude_system_ids: tuple[str, ...] = (),
    ) -> list[str]:
        return [
            str(summary["system_id"])
            for summary in HistoryStore._list_history_system_summaries(
                connection,
                exclude_system_ids=exclude_system_ids,
            )
        ]

    @staticmethod
    def _is_readonly_database_error(exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return "readonly" in message or "read-only" in message

    @staticmethod
    def _is_database_locked_error(exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database table is locked" in message

    @staticmethod
    def _is_journal_mode_fallback_error(exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return "disk i/o" in message or "readonly" in message or "read-only" in message

    @staticmethod
    def _row_to_slot_state(row: sqlite3.Row) -> SlotStateRecord:
        values = {
            column_name: row[column_name]
            for column_name in SLOT_STATE_COLUMN_NAMES
            if column_name != "last_seen_at"
        }
        for column_name in ("system_id", "enclosure_key", "slot_label"):
            values[column_name] = str(values[column_name])
        values["slot"] = int(values["slot"])
        for column_name in ("present", "identify_active"):
            values[column_name] = bool(values[column_name])
        return SlotStateRecord(**values)
