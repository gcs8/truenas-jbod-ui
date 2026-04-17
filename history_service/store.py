from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from history_service.domain import MetricSample, SlotEvent, SlotStateRecord

logger = logging.getLogger(__name__)

SLOT_STATE_OPTIONAL_COLUMNS: dict[str, str] = {
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
    details_json TEXT NOT NULL
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
    state TEXT
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
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as connection:
                connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                connection.executescript(SCHEMA)
                self._ensure_slot_state_columns(connection)
                connection.commit()
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
            with closing(self._connect()) as connection:
                connection.executescript(SCHEMA)
                self._ensure_slot_state_columns(connection)
                connection.commit()

    @staticmethod
    def _ensure_slot_state_columns(connection: sqlite3.Connection) -> None:
        existing_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(slot_state_current)").fetchall()
        }
        for column_name, column_type in SLOT_STATE_OPTIONAL_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(
                f"ALTER TABLE slot_state_current ADD COLUMN {column_name} {column_type}"
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
                temp_path.replace(final_path)
                self._prune_backup_snapshots(backup_root, retention_count)
            finally:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)

        return final_path

    @staticmethod
    def _backup_stamp(snapshot_label: str | None) -> str:
        if snapshot_label:
            try:
                observed_at = datetime.fromisoformat(snapshot_label)
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                return observed_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            except ValueError:
                pass
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

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
        with self._lock:
            with closing(self._connect()) as connection:
                connection.execute(
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                connection.commit()

    def insert_events(self, events: list[SlotEvent]) -> None:
        if not events:
            return
        with self._lock:
            with closing(self._connect()) as connection:
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
                        details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        )
                        for item in events
                    ],
                )
                connection.commit()

    def insert_metric_samples(self, samples: list[MetricSample]) -> None:
        if not samples:
            return
        with self._lock:
            with closing(self._connect()) as connection:
                connection.executemany(
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
                        state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        )
                        for item in samples
                    ],
                )
                connection.commit()

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

        payload: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["value"] = item["value_integer"] if item["value_integer"] is not None else item["value_real"]
            payload.append(item)
        return payload

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
