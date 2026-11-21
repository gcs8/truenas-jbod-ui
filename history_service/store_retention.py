"""Retention SQL helpers that run on a connection `HistoryStore` owns.

`HistoryStore` opens and closes every connection, holds its locks and commits
or rolls back every retention transaction; the helpers here only build
retention SQL and run statements on the connection they are given.
`HistoryStore` keeps a method under each of these names, with the same kind,
signature and defaults, that passes its own module in as `store_module` (and
its class as `cls` for the class methods). Every store-module name, such as
`datetime`, `timedelta`, `_history_utc_instant` and
`_RETENTION_FLOOR_TEXT_MAX_BYTES`, is looked up on `store_module` at call
time, and sibling helpers are called through `cls`, so a replacement made on
the store module or class still reaches this code. This module does not
import the store module. Multi-line SQL keeps the indentation it had in the
class, so every statement is byte-for-byte the released text, except that
rollup buckets are named from julianday() (#1320). The bounded-page
statements of _remove_retention_page are new (#1268); retention batches use
them, and the earlier per-table helpers keep their released text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from types import ModuleType
from typing import Any


def _register_retention_functions(store_module: ModuleType, connection: sqlite3.Connection) -> None:
    # Retention passes stored text as a BLOB in the database's encoding,
    # so text that does not decode reaches here instead of failing the
    # statement; like any other text that is not a time, it has none.
    encoding = connection.execute("PRAGMA encoding").fetchone()[0]

    def utc_instant(value: object) -> str | None:
        try:
            text = value.decode(encoding) if isinstance(value, bytes) else value
        except UnicodeDecodeError:
            return None
        return store_module._history_utc_instant(text)

    connection.create_function("_history_utc_instant", 1, utc_instant, deterministic=True)


def _newer_last_sample(store_module: ModuleType, new: str, old: str) -> str:
    """SQL: whether the sample at time text `new` gives a bucket's last value over `old`.

    One total order, the same as the raw-sample rank in
    _roll_up_metric_rows: SQLite's own reading, which also names the
    bucket, then the stored text as before #1204; a later arrival wins
    where both are equal, as the higher row id does in the rank. So the
    last value does not depend on how the samples were batched.

    SQLite's reading rounds to about a millisecond, which can tie two
    samples but never puts an earlier one after a later one. Inside one
    such millisecond the text decides, even between two times that each
    name an exact instant, so a sample a fraction of a millisecond older
    can give the last value when its text sorts last. The service writes
    one timestamp format, whose text sorts in time order, and never
    samples one counter twice in a millisecond: only restored or imported
    text can meet this.

    Text SQLite does not read (a NULL reading) is older than any text it
    reads, as NULL sorts in the rank; two such texts compare as text. A
    raw sample with one has no bucket, so only the last time of a kept
    or restored bucket can be one, and the first readable sample merged
    into that bucket replaces it.
    """

    day_new, day_old = f"julianday({new})", f"julianday({old})"
    return (
        f"CASE WHEN {day_new} > {day_old} THEN 1 WHEN {day_new} < {day_old} THEN 0 "
        f"WHEN ({day_new} IS NULL) <> ({day_old} IS NULL) THEN {day_old} IS NULL "
        f"ELSE {new} >= {old} END"
    )


def _retention_bounds(store_module: ModuleType, cutoff: str) -> tuple[str, str, str | None]:
    """Return (cutoff, settled bound, cutoff instant) for a retention predicate.

    Stored timestamps may carry any UTC offset, so their text order is not
    their time order (#1204). A row is expired when its text sorts before
    the cutoff AND either its text sorts before the settled bound or its
    exact UTC instant is before the cutoff's.

    The settled bound is the date one day before the cutoff. A UTC offset
    is less than 24 hours, so a row whose text starts with an earlier date
    is older than the cutoff whatever its offset; those rows need no parse,
    exactly as before. Only rows in the last day or so before the cutoff
    are parsed, so a batch never reads the rows newer than the cutoff, and
    a row whose time cannot be read is kept only until it ages past the
    settled bound. A row stored with a positive offset whose text still
    sorts after the cutoff is expired on a later pass, never early.
    """

    settled = (store_module.datetime.fromisoformat(cutoff) - store_module.timedelta(days=1)).date().isoformat()
    return cutoff, settled, store_module._history_utc_instant(cutoff)


def _expired_timestamp_predicate(
    store_module: ModuleType,
    timestamp_column: str,
    key_column: str = "id",
    *,
    floored: bool,
) -> str:
    # Parameters: _retention_parameters(cutoff, floor). The first comparison
    # keeps the scan on the timestamp index and bounded by the cutoff. With
    # a floor, the last one starts the scan after it. Without one there is
    # no lower bound at all: no sentinel value, which a legal row could
    # equal and so be skipped.
    predicate = (
        f"{timestamp_column} < ? AND ({timestamp_column} < ? "
        f"OR _history_utc_instant(CAST({timestamp_column} AS BLOB)) < ?)"
    )
    if floored:
        predicate += f" AND ({timestamp_column}, {key_column}) > (CAST(? AS TEXT), ?)"
    return predicate


def _retention_parameters(
    store_module: ModuleType,
    cls,
    cutoff: str,
    floor: tuple[bytes | str, int] | None,
) -> tuple[Any, ...]:
    return (*cls._retention_bounds(cutoff), *(floor or ()))


def _expired_rows_subquery(
    store_module: ModuleType, cls, table_name: str, timestamp_column: str, *, floored: bool
) -> str:
    # Parameters: _retention_parameters(cutoff, floor), then the batch size.
    return (
        f"SELECT id FROM {table_name} "
        f"WHERE {cls._expired_timestamp_predicate(timestamp_column, floored=floored)} "
        f"ORDER BY {timestamp_column}, id LIMIT ?"
    )


def _deleted_floor_text(store_module: ModuleType, timestamp_column: str) -> str:
    """RETURNING expression: the deleted row's timestamp text, or NULL when too long."""

    text = f"CAST({timestamp_column} AS BLOB)"
    return f"CASE WHEN length({text}) <= {store_module._RETENTION_FLOOR_TEXT_MAX_BYTES} THEN {text} END"


def _advance_scan_floor(
    store_module: ModuleType,
    connection: sqlite3.Connection,
    floors: dict[str, tuple[bytes | str, int]],
    name: str,
    deleted: list[Any],
) -> int:
    # Every row before a deleted one (by text, then key) was read by this
    # batch: deleted, or rejected by the exact-time test. The floor is the
    # last deleted row whose text was short enough to be read back; leaving
    # the others out can only keep the floor lower.
    named = [(bytes(row[0]), int(row[1])) for row in deleted if row[0] is not None]
    if named:
        text, key = max(named)
        encoding = connection.execute("PRAGMA encoding").fetchone()[0]
        if encoding != "UTF-8":
            # CAST(? AS TEXT) reads bound bytes as UTF-8, so a UTF-16 floor
            # is bound as its string. Bytes that do not decode cannot be
            # named: keep the lower floor and read those rows again.
            try:
                text = text.decode(encoding)
            except UnicodeDecodeError:
                return len(deleted)
        floors[name] = (text, key)
    return len(deleted)


def _delete_expired_rows(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    *,
    table_name: str,
    timestamp_column: str,
    cutoff: str | None,
    batch_size: int,
    floors: dict[str, tuple[bytes | str, int]],
) -> int:
    if cutoff is None:
        return 0
    floor = floors.get(table_name)
    subquery = cls._expired_rows_subquery(table_name, timestamp_column, floored=floor is not None)
    deleted = connection.execute(
        f"DELETE FROM {table_name} WHERE id IN ({subquery}) "
        f"RETURNING {cls._deleted_floor_text(timestamp_column)}, id",
        (*cls._retention_parameters(cutoff, floor), batch_size),
    ).fetchall()
    return cls._advance_scan_floor(connection, floors, table_name, deleted)


def _roll_up_metric_rows(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    *,
    cutoff: str,
    batch_size: int,
    bucket_seconds: int,
    floor: tuple[bytes | str, int] | None,
    rollup_cutoff: str | None = None,
) -> list[int]:
    """Roll the selected raw rows up; return the written rollups already expired.

    Expiry is the rollup table's own predicate, evaluated on each row the
    statement inserted or updated.
    """

    expired_rows = cls._expired_rows_subquery("metric_samples", "observed_at", floored=floor is not None)
    return cls._roll_up_selected_rows(
        connection,
        selected_rows=expired_rows,
        selected_parameters=(*cls._retention_parameters(cutoff, floor), batch_size),
        bucket_seconds=bucket_seconds,
        rollup_cutoff=rollup_cutoff,
    )


def _roll_up_selected_rows(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    *,
    selected_rows: str,
    selected_parameters: tuple[Any, ...],
    bucket_seconds: int,
    rollup_cutoff: str | None = None,
) -> list[int]:
    """Roll up the raw rows whose ids `selected_rows` returns; return the written rollups already expired.

    `selected_rows` is a SELECT of metric_samples ids and `selected_parameters`
    are its parameters. Expiry is the rollup table's own predicate, evaluated
    on each row the statement inserted or updated.
    """

    if rollup_cutoff is None:
        returning, returning_parameters = "RETURNING rowid, 0", ()
    else:
        returning = (
            "RETURNING rowid, "
            + cls._expired_timestamp_predicate("bucket_start", "rowid", floored=False)
        )
        returning_parameters = cls._retention_bounds(rollup_cutoff)
    # The bucket is named from julianday(), the same reading that ranks the
    # samples below. strftime() on the text can name the hour before the
    # one julianday() reads for a time in the last half millisecond of an
    # hour, depending on the SQLite build and the UTC offset spelling (#1320).
    if bucket_seconds == 3600:
        bucket_expression = "strftime('%Y-%m-%dT%H:00:00+00:00', julianday(observed_at))"
    elif bucket_seconds == 86400:
        bucket_expression = "strftime('%Y-%m-%dT00:00:00+00:00', julianday(observed_at))"
    else:
        raise ValueError("Unsupported history rollup interval.")
    # The last value's order (see _newer_last_sample): SQLite's reading,
    # the text, then the row id. Rows without a value are not ranked.
    partition = (
        f"{bucket_expression}, system_id, enclosure_key, slot, "
        "metric_name, COALESCE(disk_identity_key, '')"
    )
    newer = cls._newer_last_sample("excluded.last_observed_at", "metric_rollups.last_observed_at")
    written = connection.execute(
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
                MAX(CASE WHEN rollup_rank = 1 THEN observed_at END),
                MAX(device_name), MAX(serial), MAX(model), MAX(state),
                MAX(gptid), MAX(persistent_id_label),
                COALESCE(disk_identity_key, ''), MAX(logical_unit_id), MAX(sas_address)
            FROM (
                SELECT
                    metric_samples.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY {partition}
                        ORDER BY julianday(observed_at) DESC, observed_at DESC, id DESC
                    ) AS rollup_rank
                FROM metric_samples
                WHERE id IN ({selected_rows})
                  AND COALESCE(value_real, CAST(value_integer AS REAL)) IS NOT NULL
            ) selected_samples
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
                last_value = CASE WHEN {newer}
                    THEN excluded.last_value ELSE metric_rollups.last_value END,
                last_observed_at = CASE WHEN {newer}
                    THEN excluded.last_observed_at ELSE metric_rollups.last_observed_at END,
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
            """
        + returning,
        (
            bucket_seconds,
            *selected_parameters,
            *returning_parameters,
        ),
    ).fetchall()
    return [int(row[0]) for row in written if row[1]]


def _delete_rollup_batch(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    *,
    bucket_seconds: int,
    cutoff: str | None,
    batch_size: int,
    floors: dict[str, tuple[bytes | str, int]],
) -> int:
    if cutoff is None:
        return 0
    name = f"rollups_{bucket_seconds}"
    floor = floors.get(name)
    deleted = connection.execute(
        f"""
            DELETE FROM metric_rollups
            WHERE rowid IN (
                SELECT rowid
                FROM metric_rollups
                WHERE bucket_seconds = ?
                  AND {cls._expired_timestamp_predicate('bucket_start', 'rowid', floored=floor is not None)}
                ORDER BY bucket_start, rowid
                LIMIT ?
            )
            RETURNING {cls._deleted_floor_text('bucket_start')}, rowid
            """,
        (bucket_seconds, *cls._retention_parameters(cutoff, floor), batch_size),
    ).fetchall()
    return cls._advance_scan_floor(connection, floors, name, deleted)


def _retention_rows_exist(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    table_name: str,
    timestamp_column: str,
    cutoff: str | None,
    *,
    floor: tuple[bytes | str, int] | None = None,
) -> bool:
    if cutoff is None:
        return False
    row = connection.execute(
        f"SELECT EXISTS(SELECT 1 FROM {table_name} "
        f"WHERE {cls._expired_timestamp_predicate(timestamp_column, floored=floor is not None)} LIMIT 1)",
        cls._retention_parameters(cutoff, floor),
    ).fetchone()
    return bool(row[0])


def _rollups_exist(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    bucket_seconds: int,
    cutoff: str | None,
    *,
    floor: tuple[bytes | str, int] | None = None,
) -> bool:
    if cutoff is None:
        return False
    row = connection.execute(
        f"""
            SELECT EXISTS(
                SELECT 1
                FROM metric_rollups
                WHERE bucket_seconds = ?
                  AND {cls._expired_timestamp_predicate('bucket_start', 'rowid', floored=floor is not None)}
                LIMIT 1
            )
            """,
        (bucket_seconds, *cls._retention_parameters(cutoff, floor)),
    ).fetchone()
    return bool(row[0])


def _remove_retention_page(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    *,
    name: str,
    table_name: str,
    timestamp_column: str,
    key_column: str,
    cutoff: str,
    batch_size: int,
    floors: dict[str, tuple[bytes | str, int]],
    scope: tuple[str, tuple[Any, ...]] = ("", ()),
    roll_up: Callable[[str, tuple[Any, ...]], None] | None = None,
) -> tuple[int, bool]:
    """Remove up to batch_size expired rows from one bounded page; return (removed, finished).

    The page is the next rows whose text sorts before the cutoff, after the
    table's floor, in index order, at most _RETENTION_PAGE_FACTOR times the
    batch size; no other candidate is read, however many rows the exact-time
    test keeps (#1268). The first batch_size expired rows of the page are
    rolled up (`roll_up`, given their ids as a SELECT and its parameters) and
    deleted, and the floor moves past every row the batch read and did not
    leave for the next batch, kept rows included. `scope` is an extra
    equality prefix of the index ("bucket_seconds = ? AND ", parameters).
    finished: nothing after the floor can be eligible, because every
    candidate after it was read with this cutoff, or none is left. With a
    batch_size of 0 nothing is read but that one-row check.
    """

    page_size = store_module._RETENTION_PAGE_FACTOR * batch_size
    _, settled, instant = cls._retention_bounds(cutoff)
    scope_sql, scope_parameters = scope

    def candidates(floor: tuple[bytes | str, int] | None) -> tuple[str, tuple[Any, ...]]:
        after = f" AND ({timestamp_column}, {key_column}) > (CAST(? AS TEXT), ?)" if floor else ""
        return (
            f"FROM {table_name} WHERE {scope_sql}{timestamp_column} < ?{after}",
            (*scope_parameters, cutoff, *(floor or ())),
        )

    source, parameters = candidates(floors.get(name))
    # CASE, not OR: the exact-time function runs only for rows in the day
    # before the cutoff, as in the expiry predicate.
    page = connection.execute(
        f"SELECT {cls._deleted_floor_text(timestamp_column)}, {key_column}, "
        f"CASE WHEN {timestamp_column} < ? THEN 1 "
        f"WHEN _history_utc_instant(CAST({timestamp_column} AS BLOB)) < ? THEN 1 ELSE 0 END "
        f"{source} ORDER BY {timestamp_column}, {key_column} LIMIT ?",
        (settled, instant, *parameters, page_size),
    ).fetchall()
    keys: list[int] = []
    processed = len(page)
    for index, (_, key, expired) in enumerate(page):
        if expired:
            if len(keys) == batch_size:
                processed = index
                break
            keys.append(int(key))
    if keys:
        selected = ("SELECT value FROM json_each(?)", (json.dumps(keys),))
        if roll_up is not None:
            roll_up(*selected)
        connection.execute(f"DELETE FROM {table_name} WHERE {key_column} IN ({selected[0]})", selected[1])
    cls._advance_scan_floor(connection, floors, name, page[:processed])
    if processed < len(page):
        return len(keys), False
    if len(page) < page_size:
        return len(keys), True
    source, parameters = candidates(floors.get(name))
    remaining = connection.execute(f"SELECT EXISTS(SELECT 1 {source})", parameters).fetchone()[0]
    return len(keys), not remaining
