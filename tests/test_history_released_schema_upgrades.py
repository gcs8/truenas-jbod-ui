"""Upgrades from real released history schemas, including a kill at each phase (#416).

The fixtures in ``tests/fixtures/history_released_schemas/`` are the exact
``SCHEMA`` strings from ``history_service/store.py`` at the release tags
v0.8.0, v0.21.2 and v0.22.2 (v0.23.0 has the same text as v0.22.2). The tests
here check three things:

* Every supported predecessor upgrades, and reopening it afterwards is
  idempotent. Row contents, identity keys, the counter table and
  ``quick_check`` survive.
* A real subprocess killed with ``os._exit`` right after each startup
  migration phase leaves a database that the next start finishes, ending in
  exactly the same state as an uninterrupted upgrade.
* A database this build upgraded still takes the released v0.22.2 ``SCHEMA``
  and an old-shaped insert, so the previous image can read it and keeps the
  post-upgrade rows. The same file stamped newer than this build is refused
  before any write.

All databases are synthetic and live in a temporary directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import closing
from pathlib import Path

from history_service.startup import HistorySchemaVersionError
from history_service.store import CURRENT_SCHEMA_VERSION, HistoryStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "history_released_schemas"
RELEASED_SCHEMAS = ("v0.8.0", "v0.21.2", "v0.22.2")

requires_posix_migration_lock = unittest.skipUnless(
    importlib.util.find_spec("fcntl") is not None,
    "History migration locking requires POSIX flock support.",
)

# Columns every released shape has; the builder fills whatever else exists.
SLOTS = 6
EVENTS_PER_SLOT = 3
SAMPLES_PER_SLOT = 40  # 240 samples, so a batch size of 50 needs several batches.


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def _value(column: str, table: str, slot: int, index: int) -> object:
    serial = f"SER-{slot:03d}"
    fixed: dict[str, object] = {
        "system_id": "archive-core",
        "system_label": "Archive",
        "enclosure_key": "enc-a",
        "enclosure_id": "enc-a",
        "enclosure_label": "Shelf A",
        "slot": slot,
        "slot_label": f"Slot {slot:02d}",
        "present": 1,
        "identify_active": 0,
        "state": "healthy",
        "device_name": f"da{slot}",
        "serial": serial,
        "model": "Example Drive",
        "gptid": f"gptid/{slot:08x}",
        "persistent_id_label": "GPTID",
        "details_json": "{}",
        "event_type": "disk_inserted",
        "metric_name": ("temperature_c", "bytes_read")[index % 2],
        "value_integer": 30 + index % 9,
        "last_seen_at": "2026-08-01T00:00:00+00:00",
        "observed_at": f"2026-08-01T{index // 60:02d}:{index % 60:02d}:00+00:00",
        "bucket_start": f"2026-08-01T{index:02d}:00:00+00:00",
        "bucket_seconds": 3600,
        "sample_count": 4,
        "value_sum": 120.0,
        "value_min": 29.0,
        "value_max": 31.0,
        "last_value": 30.0,
        "last_observed_at": "2026-08-01T00:59:00+00:00",
        "disk_identity_key": "",
    }
    return fixed.get(column)


def build_released_database(path: Path, release: str, *, stamp_like_release: bool = False) -> None:
    """A synthetic database from a released SCHEMA text, filled with example rows.

    `stamp_like_release` also does what that release's own startup would
    already have done (v0.22.2: user_version 1, identity keys, seeded
    counters), so both a raw and an already-started predecessor are covered.
    """

    schema = (FIXTURES / f"{release}.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL").fetchall()
        connection.executescript(schema)

        def insert(table: str, slot: int, index: int) -> None:
            columns = [
                column
                for column in _columns(connection, table)
                if column != "id" and _value(column, table, slot, index) is not None
            ]
            if table != "metric_rollups":
                columns = [column for column in columns if column != "disk_identity_key"]
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                [_value(column, table, slot, index) for column in columns],
            )

        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for slot in range(SLOTS):
            insert("slot_state_current", slot, 0)
            for index in range(EVENTS_PER_SLOT):
                insert("slot_events", slot, index)
            for index in range(SAMPLES_PER_SLOT):
                insert("metric_samples", slot, index)
            if "metric_rollups" in tables:
                for index in range(2):
                    insert("metric_rollups", slot, index)
        if stamp_like_release:
            for table in ("slot_state_current", "slot_events", "metric_samples"):
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET disk_identity_key = lower(trim(serial)) || '|' ||
                        lower(trim(coalesce(nullif(persistent_id_label, ''), 'unknown'))) || '|' ||
                        lower(trim(gptid))
                    WHERE serial IS NOT NULL AND gptid IS NOT NULL
                    """
                )
            for table in ("slot_events", "metric_samples", "metric_rollups"):
                connection.execute(
                    f"INSERT INTO history_table_counts VALUES (?, (SELECT COUNT(*) FROM {table}))",
                    (table,),
                )
            connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def original_rows(path: Path) -> dict[str, str]:
    """A digest of every row as the release wrote it, keyed by table."""

    digests: dict[str, str] = {}
    with closing(sqlite3.connect(path)) as connection:
        for table in ("slot_state_current", "slot_events", "metric_samples"):
            columns = [column for column in _columns(connection, table) if column != "disk_identity_key"]
            digest = hashlib.sha256()
            for row in connection.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
            ).fetchall():
                digest.update(repr(row).encode())
            digests[table] = digest.hexdigest()
    return digests


def original_columns(path: Path) -> dict[str, list[str]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: [column for column in _columns(connection, table) if column != "disk_identity_key"]
            for table in ("slot_state_current", "slot_events", "metric_samples")
        }


def end_state(path: Path, columns: dict[str, list[str]]) -> dict[str, object]:
    """Everything an upgrade must preserve or produce, read without writing."""

    with closing(sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)) as connection:
        state: dict[str, object] = {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "schema": sorted(
                (str(row[0]), str(row[1]), str(row[2] or ""))
                for row in connection.execute("SELECT type, name, sql FROM sqlite_master")
            ),
            "counters": dict(connection.execute("SELECT table_name, row_count FROM history_table_counts")),
            "real_counts": {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("slot_events", "metric_samples", "metric_rollups")
            },
            "identity_keys": {
                table: connection.execute(
                    f"SELECT rowid, disk_identity_key FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in ("slot_state_current", "slot_events", "metric_samples")
            },
        }
        for table, table_columns in columns.items():
            digest = hashlib.sha256()
            for row in connection.execute(
                f"SELECT {', '.join(table_columns)} FROM {table} ORDER BY rowid"
            ).fetchall():
                digest.update(repr(row).encode())
            state[f"rows:{table}"] = digest.hexdigest()
    return state


# Each phase is a HistoryStore method; the child exits with os._exit right
# after the named call returns (or on the Nth call), with no commit, cleanup
# or close, as a container kill would.
KILL_PHASES = {
    "after slot_state column adds": ("_ensure_slot_state_columns", 1),
    "after slot_events column adds": ("_ensure_slot_event_columns", 1),
    "after metric_samples column adds": ("_ensure_metric_sample_columns", 1),
    "inside the first backfill batch": ("_backfill_disk_identity_batch", 1),
    "after one committed backfill batch": ("_backfill_disk_identity_batch", 2),
    "after the backfill and version stamp": ("_backfill_disk_identity_keys_once", 1),
    "after the identity index build": ("_ensure_identity_indexes", 1),
    "after the counter sync, before the final commit": ("_synchronize_table_counts", 1),
}

KILL_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {root!r})
    from history_service import store as store_module
    from history_service.store import HistoryStore

    store_module.DISK_IDENTITY_BACKFILL_BATCH_SIZE = 50
    name, kill_on_call = {method!r}, {call!r}
    original = getattr(HistoryStore, name)
    calls = []

    def killed(*args, **kwargs):
        # The *batch* phase dies inside the batch on its Nth call: earlier
        # batches are committed, this one is not.
        calls.append(1)
        if name == "_backfill_disk_identity_batch" and len(calls) == kill_on_call:
            original(*args, **kwargs)
            os._exit(17)
        result = original(*args, **kwargs)
        if name != "_backfill_disk_identity_batch" and len(calls) == kill_on_call:
            os._exit(17)
        return result

    setattr(HistoryStore, name, staticmethod(killed))
    HistoryStore({path!r})
    os._exit(0)
    """
)


@requires_posix_migration_lock
class ReleasedSchemaUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)

    def _database(self, name: str, release: str, **kwargs: bool) -> Path:
        path = self.directory / name / "history.sqlite3"
        path.parent.mkdir()
        build_released_database(path, release, **kwargs)
        return path

    def test_fixtures_are_the_released_schema_text(self) -> None:
        # Pinned so an accidental edit cannot turn a fixture into today's shape.
        expected = {
            "v0.8.0": ("slot_state_current", "slot_events", "metric_samples"),
            "v0.21.2": ("slot_state_current", "slot_events", "metric_samples"),
            "v0.22.2": (
                "slot_state_current",
                "slot_events",
                "metric_samples",
                "metric_rollups",
                "history_table_counts",
                "history_maintenance_state",
            ),
        }
        for release, tables in expected.items():
            with self.subTest(release=release):
                path = self._database(f"shape-{release}", release)
                with closing(sqlite3.connect(path)) as connection:
                    found = tuple(
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
                        )
                    )
                    self.assertEqual(found, tables)
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
        with closing(sqlite3.connect(self._database("v080-columns", "v0.8.0"))) as connection:
            self.assertNotIn("gptid", _columns(connection, "metric_samples"))
            self.assertNotIn("disk_identity_key", _columns(connection, "slot_state_current"))

    def test_every_released_schema_upgrades_and_reopens_idempotently(self) -> None:
        cases = [(release, False) for release in RELEASED_SCHEMAS] + [("v0.22.2", True)]
        for release, stamped in cases:
            with self.subTest(release=release, started_by_release=stamped):
                path = self._database(f"up-{release}-{stamped}", release, stamp_like_release=stamped)
                rows_before = original_rows(path)
                columns = original_columns(path)

                HistoryStore(str(path))
                first = end_state(path, columns)
                HistoryStore(str(path))
                second = end_state(path, columns)

                self.assertEqual(first, second, "a second start must change nothing")
                self.assertEqual(first["quick_check"], "ok")
                self.assertEqual(first["user_version"], CURRENT_SCHEMA_VERSION)
                self.assertEqual(first["counters"], first["real_counts"])
                self.assertEqual(
                    first["real_counts"]["metric_samples"], SLOTS * SAMPLES_PER_SLOT
                )
                self.assertEqual(first["real_counts"]["slot_events"], SLOTS * EVENTS_PER_SLOT)
                for table, digest in rows_before.items():
                    self.assertEqual(first[f"rows:{table}"], digest, table)
                with closing(sqlite3.connect(path)) as connection:
                    keyed_tables = [
                        table
                        for table in ("slot_state_current", "slot_events", "metric_samples")
                        if release != "v0.8.0" or table == "slot_state_current"
                    ]
                    for table in keyed_tables:
                        unkeyed = connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE disk_identity_key IS NULL "
                            "OR disk_identity_key = ''"
                        ).fetchone()[0]
                        self.assertEqual(unkeyed, 0, f"{release} {table} rows left without a key")
                    key = connection.execute(
                        "SELECT disk_identity_key FROM slot_state_current WHERE slot = 1"
                    ).fetchone()[0]
                    expected_label = "unknown" if release == "v0.8.0" else "gptid"
                    self.assertEqual(key, f"ser-001|{expected_label}|gptid/00000001")

    def test_a_kill_after_each_migration_phase_finishes_on_the_next_start(self) -> None:
        for release in ("v0.8.0", "v0.21.2"):
            reference = self._database(f"reference-{release}", release)
            columns = original_columns(reference)
            HistoryStore(str(reference))
            expected = end_state(reference, columns)
            for phase, (method, call) in KILL_PHASES.items():
                if release == "v0.8.0" and method == "_backfill_disk_identity_batch" and call == 2:
                    continue  # v0.8.0 rows without gptid have no backfill; one batch covers the rest.
                with self.subTest(release=release, phase=phase):
                    slug = f"kill-{release}-{method}-{call}"
                    path = self._database(slug, release)
                    child = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            KILL_CHILD.format(root=str(ROOT), method=method, call=call, path=str(path)),
                        ],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    )
                    self.assertEqual(child.returncode, 17, child.stderr[-2000:])

                    HistoryStore(str(path))

                    self.assertEqual(end_state(path, columns), expected, phase)

    def test_upgraded_database_still_takes_the_previous_release_schema(self) -> None:
        # Rollback today: every migration is additive, so the v0.22.2/v0.23.0
        # image re-runs its SCHEMA against a file this build upgraded and keeps
        # both old and post-upgrade rows. A future incompatible change needs an
        # owner decision (restore the pre-upgrade backup or ship a down-migration).
        path = self._database("rollback", "v0.21.2")
        store = HistoryStore(str(path))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, slot_label, "
                "event_type, details_json, serial, gptid) VALUES "
                "('2026-09-01T00:00:00+00:00', 'archive-core', 'enc-a', 1, 'Slot 01', "
                "'post_upgrade', '{}', 'SER-001', 'gptid/00000001')"
            )
            connection.commit()
        del store
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript((FIXTURES / "v0.22.2.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO metric_samples (observed_at, system_id, enclosure_key, slot, slot_label, "
                "metric_name, value_integer) VALUES ('2026-09-01T00:05:00+00:00', 'archive-core', "
                "'enc-a', 1, 'Slot 01', 'temperature_c', 33)"
            )
            connection.commit()
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM slot_events WHERE event_type='post_upgrade'").fetchone()[0],
                1,
            )
            self.assertLessEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            counters = dict(connection.execute("SELECT table_name, row_count FROM history_table_counts"))
            self.assertEqual(
                counters["metric_samples"],
                connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0],
            )

    def test_a_released_database_stamped_newer_than_this_build_is_refused_untouched(self) -> None:
        path = self._database("future", "v0.22.2", stamp_like_release=True)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
            connection.execute("PRAGMA journal_mode=DELETE").fetchall()
        before = {
            candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest()
            for candidate in path.parent.iterdir()
        }

        with self.assertRaises(HistorySchemaVersionError):
            HistoryStore(str(path))

        after = {
            candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest()
            for candidate in path.parent.iterdir()
            if candidate.is_file()
        }
        self.assertEqual(after, before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
