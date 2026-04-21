from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from history_service.domain import MetricSample, SlotEvent, SlotStateRecord

logger = logging.getLogger(__name__)
SQLITE_SHARED_DIR_MODE = 0o777
SQLITE_SHARED_FILE_MODE = 0o666
SQLITE_TEMP_STORE = "MEMORY"
SQLITE_CACHE_SIZE_KIB = 16384

SLOT_STATE_OPTIONAL_COLUMNS: dict[str, str] = {
    "persistent_id_label": "TEXT",
    "disk_identity_key": "TEXT",
    "logical_unit_id": "TEXT",
    "sas_address": "TEXT",
    "topology_label": "TEXT",
    "multipath_device": "TEXT",
    "multipath_mode": "TEXT",
    "multipath_state": "TEXT",
    "multipath_lunid": "TEXT",
    "multipath_primary_path": "TEXT",
    "multipath_alternate_path": "TEXT",
    "multipath_active_paths": "TEXT",
    "multipath_passive_paths": "TEXT",
    "multipath_failed_paths": "TEXT",
    "multipath_other_paths": "TEXT",
    "multipath_active_controllers": "TEXT",
    "multipath_passive_controllers": "TEXT",
    "multipath_failed_controllers": "TEXT",
}

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


SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_state_current (
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    present INTEGER NOT NULL,
    state TEXT,
    identify_active INTEGER NOT NULL,
    device_name TEXT,
    serial TEXT,
    model TEXT,
    gptid TEXT,
    persistent_id_label TEXT,
    disk_identity_key TEXT,
    logical_unit_id TEXT,
    sas_address TEXT,
    pool_name TEXT,
    vdev_name TEXT,
    health TEXT,
    topology_label TEXT,
    multipath_device TEXT,
    multipath_mode TEXT,
    multipath_state TEXT,
    multipath_lunid TEXT,
    multipath_primary_path TEXT,
    multipath_alternate_path TEXT,
    multipath_active_paths TEXT,
    multipath_passive_paths TEXT,
    multipath_failed_paths TEXT,
    multipath_other_paths TEXT,
    multipath_active_controllers TEXT,
    multipath_passive_controllers TEXT,
    multipath_failed_controllers TEXT,
    last_seen_at TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_slot_events_scope
    ON slot_events (system_id, enclosure_key, slot, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_metric_samples_scope
    ON metric_samples (system_id, enclosure_key, slot, metric_name, observed_at DESC);
"""


class HistoryStore:
    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.file_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA temp_store={SQLITE_TEMP_STORE}")
            connection.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_SIZE_KIB}")
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
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _initialize(self) -> None:
        self._normalize_database_permissions()
        try:
            self._initialize_schema()
        except sqlite3.OperationalError as exc:
            if self._is_readonly_database_error(exc) and self._attempt_readonly_database_repair(exc):
                self._initialize_schema()
                return
            if not self._should_recover_database(exc):
                raise
            broken_path = self._quarantine_database()
            logger.warning(
                "History database %s was unreadable; moved it to %s and created a fresh database. Error: %s",
                self.file_path,
                broken_path,
                exc,
            )
            self._initialize_schema()
        except sqlite3.Error as exc:
            if not self._should_recover_database(exc):
                raise
            broken_path = self._quarantine_database()
            logger.warning(
                "History database %s was unreadable; moved it to %s and created a fresh database. Error: %s",
                self.file_path,
                broken_path,
                exc,
            )
            self._initialize_schema()

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            connection.executescript(SCHEMA)
            self._ensure_slot_state_columns(connection)
            self._ensure_slot_event_columns(connection)
            self._ensure_metric_sample_columns(connection)
            self._backfill_disk_identity_keys(connection)
            self._ensure_identity_indexes(connection)
            connection.commit()

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
                "unable to open database file",
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
        backup_root = Path(backup_dir)
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_name = f"{self.file_path.stem}-{self._backup_stamp(snapshot_label)}.sqlite3"
        final_path = backup_root / backup_name
        temp_path = backup_root / f"{backup_name}.tmp"

        with self._lock:
            try:
                with closing(self._connect()) as source_connection, closing(sqlite3.connect(temp_path)) as backup_connection:
                    source_connection.backup(backup_connection)
                    backup_connection.commit()
                self._normalize_shared_path_permissions(temp_path)
                temp_path.replace(final_path)
                self._normalize_shared_path_permissions(final_path)
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
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)

        return final_path

    def restore_backup(self, source_path: str | Path) -> None:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Backup source {source} does not exist.")

        temp_path = self.file_path.with_suffix(f"{self.file_path.suffix}.restore")
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            with closing(sqlite3.connect(source)) as source_connection, closing(sqlite3.connect(temp_path)) as restore_connection:
                source_connection.backup(restore_connection)
                restore_connection.commit()
            self._normalize_shared_path_permissions(temp_path)

            for suffix in ("-shm", "-wal"):
                Path(f"{self.file_path}{suffix}").unlink(missing_ok=True)
            temp_path.replace(self.file_path)
            self._normalize_database_permissions()

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
            key=lambda candidate: candidate.stat().st_mtime,
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

    @staticmethod
    def _refresh_backup_copy(source_backup_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.parent / f"{target_path.name}.tmp"
        try:
            shutil.copy2(source_backup_path, temp_path)
            HistoryStore._normalize_shared_path_permissions(temp_path)
            temp_path.replace(target_path)
            HistoryStore._normalize_shared_path_permissions(target_path)
        finally:
            temp_path.unlink(missing_ok=True)

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
        self._execute_write(
            lambda connection: connection.execute(
                """
                INSERT INTO slot_state_current (
                    system_id,
                    system_label,
                    enclosure_key,
                    enclosure_id,
                    enclosure_label,
                    slot,
                    slot_label,
                    present,
                    state,
                    identify_active,
                    device_name,
                    serial,
                    model,
                    gptid,
                    persistent_id_label,
                    disk_identity_key,
                    logical_unit_id,
                    sas_address,
                    pool_name,
                    vdev_name,
                    health,
                    topology_label,
                    multipath_device,
                    multipath_mode,
                    multipath_state,
                    multipath_lunid,
                    multipath_primary_path,
                    multipath_alternate_path,
                    multipath_active_paths,
                    multipath_passive_paths,
                    multipath_failed_paths,
                    multipath_other_paths,
                    multipath_active_controllers,
                    multipath_passive_controllers,
                    multipath_failed_controllers,
                    last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(system_id, enclosure_key, slot) DO UPDATE SET
                    system_label = excluded.system_label,
                    enclosure_id = excluded.enclosure_id,
                    enclosure_label = excluded.enclosure_label,
                    slot_label = excluded.slot_label,
                    present = excluded.present,
                    state = excluded.state,
                    identify_active = excluded.identify_active,
                    device_name = excluded.device_name,
                    serial = excluded.serial,
                    model = excluded.model,
                    gptid = excluded.gptid,
                    persistent_id_label = excluded.persistent_id_label,
                    disk_identity_key = excluded.disk_identity_key,
                    logical_unit_id = excluded.logical_unit_id,
                    sas_address = excluded.sas_address,
                    pool_name = excluded.pool_name,
                    vdev_name = excluded.vdev_name,
                    health = excluded.health,
                    topology_label = excluded.topology_label,
                    multipath_device = excluded.multipath_device,
                    multipath_mode = excluded.multipath_mode,
                    multipath_state = excluded.multipath_state,
                    multipath_lunid = excluded.multipath_lunid,
                    multipath_primary_path = excluded.multipath_primary_path,
                    multipath_alternate_path = excluded.multipath_alternate_path,
                    multipath_active_paths = excluded.multipath_active_paths,
                    multipath_passive_paths = excluded.multipath_passive_paths,
                    multipath_failed_paths = excluded.multipath_failed_paths,
                    multipath_other_paths = excluded.multipath_other_paths,
                    multipath_active_controllers = excluded.multipath_active_controllers,
                    multipath_passive_controllers = excluded.multipath_passive_controllers,
                    multipath_failed_controllers = excluded.multipath_failed_controllers,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    record.system_id,
                    record.system_label,
                    record.enclosure_key,
                    record.enclosure_id,
                    record.enclosure_label,
                    record.slot,
                    record.slot_label,
                    int(record.present),
                    record.state,
                    int(record.identify_active),
                    record.device_name,
                    record.serial,
                    record.model,
                    record.gptid,
                    record.persistent_id_label,
                    record.disk_identity_key,
                    record.logical_unit_id,
                    record.sas_address,
                    record.pool_name,
                    record.vdev_name,
                    record.health,
                    record.topology_label,
                    record.multipath_device,
                    record.multipath_mode,
                    record.multipath_state,
                    record.multipath_lunid,
                    record.multipath_primary_path,
                    record.multipath_alternate_path,
                    record.multipath_active_paths,
                    record.multipath_passive_paths,
                    record.multipath_failed_paths,
                    record.multipath_other_paths,
                    record.multipath_active_controllers,
                    record.multipath_passive_controllers,
                    record.multipath_failed_controllers,
                    observed_at,
                ),
            )
        )

    def insert_events(self, events: list[SlotEvent]) -> None:
        if not events:
            return
        self._execute_write(
            lambda connection: connection.executemany(
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

    def list_slot_events(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
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
        enclosure_key = enclosure_id or ""
        where_clauses = ["system_id = ?", "enclosure_key = ?", "slot = ?"]
        parameters: list[Any] = [system_id, enclosure_key, slot]
        if metric_name:
            where_clauses.append("metric_name = ?")
            parameters.append(metric_name)
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

        return self._metric_rows_to_payload(rows)

    def list_disk_metric_samples(
        self,
        disk_identity_key: str,
        *,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_identity_key = disk_identity_key.strip()
        if not normalized_identity_key:
            return []

        where_clauses = ["disk_identity_key = ?"]
        parameters: list[Any] = [normalized_identity_key]
        if metric_name:
            where_clauses.append("metric_name = ?")
            parameters.append(metric_name)
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
        return self._metric_rows_to_payload(rows)

    def list_disk_metric_homes(
        self,
        disk_identity_key: str,
        *,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_identity_key = disk_identity_key.strip()
        if not normalized_identity_key:
            return []

        where_clauses = ["disk_identity_key = ?"]
        parameters: list[Any] = [normalized_identity_key]
        if since:
            where_clauses.append("observed_at >= ?")
            parameters.append(since)

        query = f"""
            SELECT
                system_id,
                system_label,
                enclosure_key,
                enclosure_id,
                enclosure_label,
                slot,
                slot_label,
                MIN(observed_at) AS first_seen_at,
                MAX(observed_at) AS last_seen_at,
                COUNT(*) AS sample_count
            FROM metric_samples
            WHERE {' AND '.join(where_clauses)}
            GROUP BY
                system_id,
                system_label,
                enclosure_key,
                enclosure_id,
                enclosure_label,
                slot,
                slot_label
            ORDER BY first_seen_at ASC, last_seen_at ASC, system_id ASC, enclosure_key ASC, slot ASC
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
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

    def list_scope_history(
        self,
        system_id: str,
        enclosure_id: str | None,
        *,
        slots: list[int] | None = None,
        event_limit: int = 12,
        metric_limits: dict[str, int] | None = None,
    ) -> dict[int, dict[str, Any]]:
        enclosure_key = enclosure_id or ""
        slot_numbers = sorted({int(slot) for slot in (slots or [])})
        metric_limits = metric_limits or {}
        payload_by_slot: dict[int, dict[str, Any]] = {
            slot: {
                "events": [],
                "metrics": {
                    metric_name: []
                    for metric_name in metric_limits
                },
                "sample_counts": {},
                "latest_values": {},
            }
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
                    {
                        "events": [],
                        "metrics": {
                            metric_name: []
                            for metric_name in metric_limits
                        },
                        "sample_counts": {},
                        "latest_values": {},
                    },
                )

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
                    {
                        "events": [],
                        "metrics": {
                            metric_name: []
                            for metric_name in metric_limits
                        },
                        "sample_counts": {},
                        "latest_values": {},
                    },
                )["events"].append(item)

            for metric_name, limit in metric_limits.items():
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
                        WHERE {scope_where} AND metric_name = ?
                    )
                    WHERE row_number <= ?
                    ORDER BY slot, observed_at DESC, id DESC
                    """,
                    [*parameters, metric_name, limit],
                ).fetchall()
                for row in metric_rows:
                    item = dict(row)
                    slot = int(item["slot"])
                    item["value"] = item["value_integer"] if item["value_integer"] is not None else item["value_real"]
                    item.pop("row_number", None)
                    payload_by_slot.setdefault(
                        slot,
                        {
                            "events": [],
                            "metrics": {
                                key: []
                                for key in metric_limits
                            },
                            "sample_counts": {},
                            "latest_values": {},
                        },
                    )["metrics"].setdefault(metric_name, []).append(item)

        for slot, payload in payload_by_slot.items():
            metrics = payload.setdefault("metrics", {})
            for metric_name in metric_limits:
                samples = metrics.setdefault(metric_name, [])
                payload["sample_counts"][metric_name] = len(samples)
                payload["latest_values"][metric_name] = samples[0]["value"] if samples else None

        return payload_by_slot

    def list_scopes(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
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
                    ) AS metric_sample_count
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
        with closing(self._connect()) as connection:
            tracked_slots = int(connection.execute("SELECT COUNT(*) FROM slot_state_current").fetchone()[0])
            event_count = int(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0])
            metric_sample_count = int(connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0])
        return {
            "tracked_slots": tracked_slots,
            "event_count": event_count,
            "metric_sample_count": metric_sample_count,
        }

    def list_history_system_summaries(
        self,
        exclude_system_ids: list[str] | tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        normalized_excludes = tuple(
            sorted({system_id.strip() for system_id in exclude_system_ids if system_id and system_id.strip()})
        )
        with closing(self._connect()) as connection:
            return self._list_history_system_summaries(connection, exclude_system_ids=normalized_excludes)

    def delete_system_history(self, system_id: str) -> dict[str, Any]:
        normalized_system_id = system_id.strip()
        if not normalized_system_id:
            return self._empty_cleanup_summary()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            summary = self._delete_history_for_system_ids(connection, [normalized_system_id])
            summary["removed_system_ids"] = [normalized_system_id] if summary["total_rows"] else []
            return summary

        return self._execute_write(operation)

    def purge_orphaned_history(self, valid_system_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
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
                    """
                    INSERT OR IGNORE INTO slot_state_current (
                        system_id,
                        system_label,
                        enclosure_key,
                        enclosure_id,
                        enclosure_label,
                        slot,
                        slot_label,
                        present,
                        state,
                        identify_active,
                        device_name,
                        serial,
                        model,
                        gptid,
                        persistent_id_label,
                        disk_identity_key,
                        logical_unit_id,
                        sas_address,
                        pool_name,
                        vdev_name,
                        health,
                        topology_label,
                        multipath_device,
                        multipath_mode,
                        multipath_state,
                        multipath_lunid,
                        multipath_primary_path,
                        multipath_alternate_path,
                        multipath_active_paths,
                        multipath_passive_paths,
                        multipath_failed_paths,
                        multipath_other_paths,
                        multipath_active_controllers,
                        multipath_passive_controllers,
                        multipath_failed_controllers,
                        last_seen_at
                    )
                    SELECT
                        ?,
                        ?,
                        enclosure_key,
                        enclosure_id,
                        enclosure_label,
                        slot,
                        slot_label,
                        present,
                        state,
                        identify_active,
                        device_name,
                        serial,
                        model,
                        gptid,
                        persistent_id_label,
                        disk_identity_key,
                        logical_unit_id,
                        sas_address,
                        pool_name,
                        vdev_name,
                        health,
                        topology_label,
                        multipath_device,
                        multipath_mode,
                        multipath_state,
                        multipath_lunid,
                        multipath_primary_path,
                        multipath_alternate_path,
                        multipath_active_paths,
                        multipath_passive_paths,
                        multipath_failed_paths,
                        multipath_other_paths,
                        multipath_active_controllers,
                        multipath_passive_controllers,
                        multipath_failed_controllers,
                        last_seen_at
                    FROM slot_state_current
                    WHERE system_id = ?
                    """,
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
            return {
                "source_system_id": normalized_source_id,
                "target_system_id": normalized_target_id,
                "target_system_label": normalized_target_label,
                "tracked_slots": source_slot_count,
                "event_count": event_count,
                "metric_sample_count": metric_sample_count,
                "total_rows": source_slot_count + event_count + metric_sample_count,
                "slot_state_conflicts": max(source_slot_count - max(inserted_slot_count, 0), 0),
            }

        return self._execute_write(operation)

    def _execute_write(self, operation: Any) -> Any:
        with self._lock:
            for attempt in range(2):
                try:
                    with closing(self._connect()) as connection:
                        result = operation(connection)
                        connection.commit()
                        return result
                except sqlite3.OperationalError as exc:
                    if attempt == 0 and self._is_readonly_database_error(exc) and self._attempt_readonly_database_repair(exc):
                        continue
                    raise

    def _attempt_readonly_database_repair(self, exc: sqlite3.OperationalError) -> bool:
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
        self._normalize_shared_path_permissions(self.file_path.parent, is_dir=True)
        self._normalize_shared_path_permissions(self.file_path)
        for suffix in ("-shm", "-wal"):
            self._normalize_shared_path_permissions(Path(f"{self.file_path}{suffix}"))

    @staticmethod
    def _normalize_shared_path_permissions(path: Path, *, is_dir: bool | None = None) -> None:
        if not path.exists():
            return
        if is_dir is None:
            is_dir = path.is_dir()

        try:
            current_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return

        target_mode = SQLITE_SHARED_DIR_MODE if is_dir else SQLITE_SHARED_FILE_MODE
        normalized_mode = current_mode | target_mode
        if normalized_mode == current_mode:
            return

        try:
            os.chmod(path, normalized_mode)
        except OSError as exc:
            logger.debug("Unable to normalize permissions for %s: %s", path, exc)

    @staticmethod
    def _empty_cleanup_summary() -> dict[str, Any]:
        return {
            "tracked_slots": 0,
            "event_count": 0,
            "metric_sample_count": 0,
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
        return {
            "tracked_slots": tracked_slots,
            "event_count": event_count,
            "metric_sample_count": metric_sample_count,
            "total_rows": tracked_slots + event_count + metric_sample_count,
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
                SUM(metric_sample_count) AS metric_sample_count
            FROM (
                SELECT
                    system_id,
                    MAX(system_label) AS system_label,
                    COUNT(*) AS tracked_slots,
                    0 AS event_count,
                    0 AS metric_sample_count
                FROM slot_state_current
                GROUP BY system_id
                UNION ALL
                SELECT
                    system_id,
                    MAX(system_label) AS system_label,
                    0 AS tracked_slots,
                    COUNT(*) AS event_count,
                    0 AS metric_sample_count
                FROM slot_events
                GROUP BY system_id
                UNION ALL
                SELECT
                    system_id,
                    MAX(system_label) AS system_label,
                    0 AS tracked_slots,
                    0 AS event_count,
                    COUNT(*) AS metric_sample_count
                FROM metric_samples
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
            summaries.append(
                {
                    "system_id": system_id,
                    "system_label": row["system_label"],
                    "tracked_slots": tracked_slots,
                    "event_count": event_count,
                    "metric_sample_count": metric_sample_count,
                    "total_rows": tracked_slots + event_count + metric_sample_count,
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
    def _is_journal_mode_fallback_error(exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return "disk i/o" in message or "readonly" in message or "read-only" in message

    @staticmethod
    def _row_to_slot_state(row: sqlite3.Row) -> SlotStateRecord:
        return SlotStateRecord(
            system_id=str(row["system_id"]),
            system_label=row["system_label"],
            enclosure_key=str(row["enclosure_key"]),
            enclosure_id=row["enclosure_id"],
            enclosure_label=row["enclosure_label"],
            slot=int(row["slot"]),
            slot_label=str(row["slot_label"]),
            present=bool(row["present"]),
            state=row["state"],
            identify_active=bool(row["identify_active"]),
            device_name=row["device_name"],
            serial=row["serial"],
            model=row["model"],
            gptid=row["gptid"],
            persistent_id_label=row["persistent_id_label"],
            disk_identity_key=row["disk_identity_key"],
            logical_unit_id=row["logical_unit_id"],
            sas_address=row["sas_address"],
            pool_name=row["pool_name"],
            vdev_name=row["vdev_name"],
            health=row["health"],
            topology_label=row["topology_label"],
            multipath_device=row["multipath_device"],
            multipath_mode=row["multipath_mode"],
            multipath_state=row["multipath_state"],
            multipath_lunid=row["multipath_lunid"],
            multipath_primary_path=row["multipath_primary_path"],
            multipath_alternate_path=row["multipath_alternate_path"],
            multipath_active_paths=row["multipath_active_paths"],
            multipath_passive_paths=row["multipath_passive_paths"],
            multipath_failed_paths=row["multipath_failed_paths"],
            multipath_other_paths=row["multipath_other_paths"],
            multipath_active_controllers=row["multipath_active_controllers"],
            multipath_passive_controllers=row["multipath_passive_controllers"],
            multipath_failed_controllers=row["multipath_failed_controllers"],
        )
