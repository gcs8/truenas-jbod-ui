"""Restore admission: retained timestamps must be ones retention can order (#1268).

History retention expires a row by comparing its stored timestamp text with
the cutoff, and parses the exact UTC instant only for text in the day before
the cutoff. A restored database is refused when one of those columns holds a
value that comparison cannot order. The caller passes the exact-instant
reading retention uses, so both always agree; this module imports nothing
from the store.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

# Columns whose text retention compares with its cutoff.
RETENTION_TIMESTAMP_COLUMNS = (
    ("slot_events", "observed_at"),
    ("metric_samples", "observed_at"),
    ("metric_rollups", "bucket_start"),
)
_DIGITS = "[0-9]"
_SERVICE_TIMESTAMP_PREFIX = (
    f"{_DIGITS * 4}-{_DIGITS * 2}-{_DIGITS * 2}T{_DIGITS * 2}:{_DIGITS * 2}:{_DIGITS * 2}"
)
# The forms the service writes (UTC, whole second or six fraction digits).
# Matching text with a real date and time skips the exact check in Python.
_SERVICE_TIMESTAMP_GLOBS = (
    f"{_SERVICE_TIMESTAMP_PREFIX}+00:00",
    f"{_SERVICE_TIMESTAMP_PREFIX}.{_DIGITS * 6}+00:00",
)


def retention_can_order(text: object, utc_instant: Callable[[object], str | None]) -> bool:
    """Whether retention orders this timestamp text correctly.

    It needs an exact UTC instant (an ISO-8601 date and time with an offset
    under a day) and text that starts with that time's own date: retention
    expires text more than a day before the cutoff by its date alone, and
    never reads text that sorts after the cutoff.
    """

    if not isinstance(text, str) or utc_instant(text) is None:
        return False
    return text[:10] == datetime.fromisoformat(text).date().isoformat()


def require_orderable_restored_timestamps(
    connection: sqlite3.Connection,
    utc_instant: Callable[[object], str | None],
) -> None:
    """Refuse a database with a retained timestamp retention cannot order.

    Such a row (no offset, an invalid one, another ISO-8601 layout, or text
    that is not valid in the database's encoding) would be kept or expired
    by its text alone, and every retention pass would read it again while it
    is in the day before the cutoff. Nothing is rewritten: the database is
    refused with the count and column. A table or column an older supported
    schema lacks is skipped.
    """
    registered = False
    for table_name, column_name in RETENTION_TIMESTAMP_COLUMNS:
        present = connection.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
            (table_name, column_name),
        ).fetchone()
        if present is None:
            continue
        if not registered:
            encoding = connection.execute("PRAGMA encoding").fetchone()[0]

            def can_order(value: object) -> bool:
                try:
                    text = value.decode(encoding) if isinstance(value, bytes) else value
                except UnicodeDecodeError:
                    return False
                return retention_can_order(text, utc_instant)

            connection.create_function("_history_retention_can_order", 1, can_order, deterministic=True)
            registered = True
        column = f'"{column_name}"'
        count = connection.execute(
            f'SELECT count(*) FROM "{table_name}" WHERE NOT COALESCE(({column} GLOB ? OR {column} GLOB ?) '
            f"AND strftime('%Y-%m-%dT%H:%M:%S', {column}) = substr({column}, 1, 19), 0) "
            f"AND NOT _history_retention_can_order(CAST({column} AS BLOB))",
            _SERVICE_TIMESTAMP_GLOBS,
        ).fetchone()[0]
        if count:
            raise ValueError(
                f"The history database in this backup has {count} {table_name}.{column_name} "
                f"timestamp{'' if count == 1 else 's'} that history retention cannot order: each "
                "needs an ISO-8601 date and time with a UTC offset, starting with its own date "
                "(YYYY-MM-DD). Nothing was restored and the current history was not changed."
            )
