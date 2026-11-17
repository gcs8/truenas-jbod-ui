"""Schema and index helpers that run on a connection `HistoryStore` owns.

`HistoryStore` opens and closes every connection, holds the migration lock and
commits or rolls back every transaction; the helpers here only run statements
on the connection they are given, or read the filesystem to size an index
build. `HistoryStore` keeps a method under each of these names that passes its
own module in as `store_module` (and itself as `store` for the instance
helpers). Every store-module name, including `HistoryStore`, its constants and
its logger, is looked up on `store_module` at call time, so a replacement made
on the store module or class still reaches this code. This module does not
import the store module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any


def _synchronize_table_counts(store_module: ModuleType, connection: sqlite3.Connection) -> list[str]:
    """Seed a missing counter row from a full count; triggers keep seeded rows exact.

    Returns the tables that were counted so callers and tests can see that a
    routine start touched none of the large tables.
    """

    tracked_tables = {
        str(row[0])
        for row in connection.execute("SELECT table_name FROM history_table_counts").fetchall()
    }
    # A seeded counter is trusted only while its table could hold that
    # many rows; a restored or edited file can carry any value (#1093).
    counted = store_module.HistoryStore._recount_implausible_table_counts(
        connection, tuple(name for name in store_module.HISTORY_COUNTED_TABLES if name in tracked_tables)
    )
    for table_name in store_module.HISTORY_COUNTED_TABLES:
        if table_name in tracked_tables:
            continue
        connection.execute(
            f"""
            INSERT INTO history_table_counts (table_name, row_count)
            VALUES (?, (SELECT COUNT(*) FROM {table_name}))
            ON CONFLICT (table_name) DO UPDATE SET
                row_count = excluded.row_count
            """,
            (table_name,),
        )
        counted.append(table_name)
    return counted


def _rowid_span(store_module: ModuleType, connection: sqlite3.Connection, table_name: str) -> int:
    """max(rowid) - min(rowid) + 1, or 0 for an empty table, in O(log n).

    Retained rows have distinct rowids inside that range, so no table
    holds more rows than this. Two separate queries keep SQLite's min/max
    b-tree shortcut, which a combined SELECT would lose.
    """

    high = connection.execute(f"SELECT max(rowid) FROM {table_name}").fetchone()[0]
    if high is None:
        return 0
    low = connection.execute(f"SELECT min(rowid) FROM {table_name}").fetchone()[0]
    return int(high) - int(low) + 1


def _has_table(store_module: ModuleType, connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
        ).fetchone()
        is not None
    )


def _recount_implausible_table_counts(
    store_module: ModuleType,
    connection: sqlite3.Connection, table_names: tuple[str, ...]
) -> list[str]:
    """Recount each tracked counter its table's rowid span rules out (#1093).

    The check costs two b-tree lookups per table; only a counter that is
    provably wrong pays for a full COUNT. Returns the recounted tables.
    The caller commits.
    """

    if not store_module.HistoryStore._has_table(connection, "history_table_counts"):
        return []
    recounted: list[str] = []
    for table_name in table_names:
        row = connection.execute(
            "SELECT row_count FROM history_table_counts WHERE table_name = ?", (table_name,)
        ).fetchone()
        if row is None or row[0] is None:
            continue
        bound = store_module.HistoryStore._rowid_span(connection, table_name)
        if int(row[0]) <= bound:
            continue
        store_module.logger.warning(
            "The stored row count for history table %s (%d) is more than the table can hold "
            "(at most %d); recounting it.",
            table_name,
            int(row[0]),
            bound,
        )
        store_module.HistoryStore._recount_table(connection, table_name)
        recounted.append(table_name)
    return recounted


def _recount_table(store_module: ModuleType, connection: sqlite3.Connection, table_name: str) -> None:
    connection.execute(
        f"""
        UPDATE history_table_counts
        SET row_count = (SELECT COUNT(*) FROM {table_name})
        WHERE table_name = ?
        """,
        (table_name,),
    )


def _recount_restored_table_counts(store_module: ModuleType, connection: sqlite3.Connection) -> None:
    """Replace a restored file's counters with real counts (#1093).

    A backup is outside input: its counters were never maintained by this
    service's triggers, and they size index builds. The recount reads each
    table once, like the copy that precedes it. The caller commits.
    """

    if not store_module.HistoryStore._has_table(connection, "history_table_counts"):
        return
    for table_name in store_module.HISTORY_COUNTED_TABLES:
        if store_module.HistoryStore._has_table(connection, table_name):
            store_module.HistoryStore._recount_table(connection, table_name)


def _backfill_disk_identity_keys_once(store_module: ModuleType, connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    current_version = int(row[0]) if row and row[0] is not None else 0
    if current_version >= store_module.DISK_IDENTITY_BACKFILL_USER_VERSION:
        return
    store_module.HistoryStore._backfill_disk_identity_keys(connection)
    connection.execute(f"PRAGMA user_version = {int(store_module.DISK_IDENTITY_BACKFILL_USER_VERSION)}")


def _ensure_slot_state_columns(store_module: ModuleType, connection: sqlite3.Connection) -> None:
    store_module.HistoryStore._ensure_columns(connection, "slot_state_current", store_module.SLOT_STATE_OPTIONAL_COLUMNS)


def _ensure_slot_event_columns(store_module: ModuleType, connection: sqlite3.Connection) -> None:
    store_module.HistoryStore._ensure_columns(connection, "slot_events", store_module.SLOT_EVENT_OPTIONAL_COLUMNS)


def _ensure_metric_sample_columns(store_module: ModuleType, connection: sqlite3.Connection) -> None:
    store_module.HistoryStore._ensure_columns(connection, "metric_samples", store_module.METRIC_SAMPLE_OPTIONAL_COLUMNS)


def _backfill_disk_identity_batch(
    store_module: ModuleType,
    connection: sqlite3.Connection,
    table_name: str,
    *,
    batch_size: int,
) -> int:
    cursor = connection.execute(
        f"""
        UPDATE {table_name}
        SET disk_identity_key =
            lower(trim(serial)) || '|' ||
            lower(trim(coalesce(nullif(persistent_id_label, ''), 'unknown'))) || '|' ||
            lower(trim(gptid))
        WHERE rowid IN (
            SELECT rowid
            FROM {table_name}
            WHERE {store_module._DISK_IDENTITY_BACKFILL_PENDING_SQL}
            LIMIT ?
        )
        """,
        (batch_size,),
    )
    return max(0, int(cursor.rowcount))


def _ensure_identity_indexes(store_module: ModuleType, connection: sqlite3.Connection) -> None:
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


def _missing_chronological_indexes(store_module: ModuleType, connection: sqlite3.Connection) -> tuple[str, ...]:
    present = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    return tuple(name for name, _ in store_module.CHRONOLOGICAL_INDEXES if name not in present)


def _ensure_chronological_indexes(store_module: ModuleType, connection: sqlite3.Connection) -> tuple[str, ...]:
    """Build missing chronological indexes inline when that is quick (#910).

    Returns the indexes left for the collector to build after startup. A
    large upgraded database answers its healthcheck first and keeps the
    older scan path until the background build finishes.
    """

    missing = store_module.HistoryStore._missing_chronological_indexes(connection)
    if not missing:
        return ()
    # The AUTOINCREMENT ids bound the row counts from above in O(log n).
    history_rows = sum(
        int(connection.execute(f"SELECT coalesce(max(rowid), 0) FROM {table}").fetchone()[0])
        for table in ("slot_events", "metric_samples")
    )
    if history_rows > store_module.CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS:
        store_module.logger.info(
            "Upgrading history database: %s indexes will be built in the background after "
            "startup; history reads use the older, slower path until they finish.",
            len(missing),
        )
        return missing
    statements = dict(store_module.CHRONOLOGICAL_INDEXES)
    for name in missing:
        connection.execute(statements[name])
    return ()


def _apply_bulk_maintenance_profile(store_module: ModuleType, connection: sqlite3.Connection) -> None:
    """On-disk temporary storage and a bounded page cache for one-shot bulk work (#1092)."""

    connection.execute(f"PRAGMA temp_store={store_module.BULK_MAINTENANCE_TEMP_STORE}")
    connection.execute(f"PRAGMA cache_size=-{store_module.BULK_MAINTENANCE_CACHE_SIZE_KIB}")


def _describe_free_space(store_module: ModuleType, store: Any) -> str:
    """`<free> MiB free under <dir>` for the database and temporary directories."""

    directories = [store.file_path.absolute().parent]
    temporary_directory = store.sqlite_temporary_directory()
    if temporary_directory is not None:
        directories.append(temporary_directory.absolute())
    parts: list[str] = []
    seen: set[Any] = set()
    for directory in directories:
        key = store._filesystem_key(directory)
        if key in seen:
            continue
        seen.add(key)
        try:
            free = int(store_module.shutil.disk_usage(directory).free)
        except OSError:
            continue
        parts.append(f"{free // store_module.MIB} MiB free under {store_module.host_repair_path(directory)}")
    return ("Now " + "; ".join(parts) + ".") if parts else ""


def _filesystem_key(store_module: ModuleType, directory: Path) -> tuple[int, ...] | Path:
    try:
        return (int(store_module.os.stat(directory).st_dev),)
    except OSError:
        return directory


def _index_key_columns(store_module: ModuleType, statement: str) -> tuple[str, list[str]]:
    head, _, columns = statement.partition("(")
    table = head.split()[-1]
    terms = [term.strip() for term in columns.rsplit(")", 1)[0].split(",")]
    return table, terms


def _index_build_disk_needs(
    store_module: ModuleType,
    *, index_bytes: int, freelist_bytes: int, wal_bytes: int, sorter_memory_bytes: int
) -> tuple[int, int]:
    """(database filesystem, temporary directory) bytes an index build writes.

    Every new index page goes through the WAL first, so the WAL needs the
    whole index less what the existing WAL file can absorb. Free pages
    already inside the database file reduce only its growth at the
    checkpoint, never the WAL (#1093). The sorter writes its keys to
    temporary files only when they outgrow its in-memory budget, and then
    writes all of them.
    """

    index_bytes = max(0, int(index_bytes))
    wal_growth = max(0, index_bytes - max(0, int(wal_bytes)))
    file_growth = max(0, index_bytes - max(0, int(freelist_bytes)))
    temporary = index_bytes if index_bytes > max(0, int(sorter_memory_bytes)) else 0
    return wal_growth + file_growth, temporary


def _require_index_build_space(
    store_module: ModuleType, store: Any, connection: sqlite3.Connection, statement: str
) -> None:
    """Refuse a build that provably cannot fit; the collector retries it later.

    Every need here is a lower bound, so a refusal never stops a build
    that would have fitted. A build that passes and still runs out of
    space is caught as SQLITE_FULL and handled the same way.
    """

    index_bytes = store._index_build_lower_bound_bytes(connection, statement)
    if index_bytes <= 0:
        return
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    freelist_bytes = int(connection.execute("PRAGMA freelist_count").fetchone()[0]) * page_size
    try:
        wal_bytes = int(store_module.os.stat(f"{store.file_path}-wal").st_size)
    except OSError:
        wal_bytes = 0
    database_need, temporary_need = store._index_build_disk_needs(
        index_bytes=index_bytes,
        freelist_bytes=freelist_bytes,
        wal_bytes=wal_bytes,
        sorter_memory_bytes=store_module.BULK_MAINTENANCE_CACHE_SIZE_KIB * 1024,
    )
    needs: dict[tuple[int, ...] | Path, list[Any]] = {}

    def add(directory: Path, need: int) -> None:
        entry = needs.setdefault(store._filesystem_key(directory), [directory, 0])
        entry[1] += need

    add(store.file_path.absolute().parent, database_need)
    temporary_directory = store.sqlite_temporary_directory()
    if temporary_directory is not None:
        add(temporary_directory, temporary_need)
    for directory, need in needs.values():
        if need <= 0:
            continue
        try:
            free = int(store_module.shutil.disk_usage(directory).free)
        except OSError:
            continue
        if free < need:
            store_module.logger.warning(
                "Not building a history index yet: it needs at least %d MiB free under %s "
                "for temporary files and index pages, and %d MiB is free. Collection "
                "continues and the build is retried.",
                -(-need // store_module.MIB),
                store_module.host_repair_path(directory),
                free // store_module.MIB,
            )
            raise store_module.HistoryIndexBuildSpaceError(
                "Not enough free disk space to build a history index."
            )


def _describe_startup_space_failure(store_module: ModuleType, store: Any) -> str:
    """The operator line for a startup schema upgrade that filled the volume (#1093).

    The figure is an upper estimate: no index on a table can be larger
    than the database file holding it, its WAL copy needs as much again,
    and SQLite's temporary sort files as much again.
    """

    try:
        database_bytes = int(store.file_path.stat().st_size)
    except OSError:
        database_bytes = 0
    needs: dict[tuple[int, ...] | Path, list[Any]] = {}
    for directory, need in (
        (store.file_path.absolute().parent, 2 * database_bytes),
        (store.sqlite_temporary_directory(), database_bytes),
    ):
        if directory is None:
            continue
        entry = needs.setdefault(store._filesystem_key(directory), [directory, 0])
        entry[1] += need
    parts: list[str] = []
    for directory, need in needs.values():
        try:
            free_text = f"{int(store_module.shutil.disk_usage(directory).free) // store_module.MIB} MiB is free now"
        except OSError:
            free_text = "free space could not be read"
        parts.append(
            f"up to about {-(-need // store_module.MIB)} MiB free on the volume holding "
            f"{store_module.host_repair_path(directory)} ({free_text})"
        )
    return (
        f"The history database {store_module.host_repair_path(store.file_path)} ran out of disk space "
        "while startup was upgrading it and building its indexes. SQLite rolled the "
        "change back, so the database is unchanged. The upgrade needs "
        + "; and ".join(parts)
        + ". Free space on that volume, then restart the history service."
    )


def _ensure_columns(
    store_module: ModuleType,
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
