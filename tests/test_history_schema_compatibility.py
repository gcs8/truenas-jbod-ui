from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.history_schema_fixtures import RELEASE_SCHEMAS


class SchemaCompatibilityTests(unittest.TestCase):
    def classifier(self):
        self.assertIsNotNone(
            importlib.util.find_spec("history_service.schema_compatibility"),
            "The read-only history schema classifier is not implemented",
        )
        from history_service.schema_compatibility import classify_schema
        return classify_schema

    def classify(self, connection):
        connection.execute("PRAGMA query_only=ON")
        return self.classifier()(connection)

    def test_released_families_accept_both_backfill_markers(self):
        for ref, commit, sql in RELEASE_SCHEMAS:
            for marker in (0, 1):
                with self.subTest(ref=ref, commit=commit, marker=marker):
                    with closing(sqlite3.connect(":memory:")) as connection:
                        connection.executescript(sql)
                        connection.execute(f"PRAGMA user_version={marker}")
                        result = self.classify(connection)
                        self.assertEqual(result.state, "supported")
                        self.assertEqual(result.backfill_marker, marker)

    def test_foreign_and_unsupported_markers_preserve_bytes(self):
        cases = [("CREATE TABLE private_table(secret TEXT);", 0),
                 ("CREATE TABLE private_table(secret TEXT);", 1)]
        for marker in (-1, 2, 999):
            cases.extend([("", marker), (RELEASE_SCHEMAS[-1][2], marker)])
        for sql, marker in cases:
            with self.subTest(marker=marker, foreign="private_table" in sql):
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "synthetic.sqlite3"
                    with closing(sqlite3.connect(path)) as connection:
                        connection.executescript(sql)
                        connection.execute(f"PRAGMA user_version={marker}")
                        connection.commit()
                    before = {p.name: p.read_bytes() for p in Path(temp).iterdir()}
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                        classify = self.classifier()
                        from history_service.schema_compatibility import SchemaIncompatibility
                        connection.execute("PRAGMA query_only=ON")
                        with self.assertRaises(SchemaIncompatibility) as caught:
                            classify(connection)
                        self.assertNotIn(temp, str(caught.exception))
                        self.assertNotIn("private_table", str(caught.exception))
                    self.assertEqual(before, {p.name: p.read_bytes() for p in Path(temp).iterdir()})

    def test_empty_schema_is_not_claimed_as_first_install(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            self.assertEqual(self.classify(connection).state, "empty")

    def test_each_released_ddl_and_alter_prefix_is_enumerated(self):
        import json
        contracts = json.loads((Path(__file__).resolve().parents[1] /
                                "history_service/schema_contracts.json").read_text())
        checkpoints = 0
        for index, contract in enumerate(contracts):
            for predecessor in (None, *RELEASE_SCHEMAS[:index]):
                with closing(sqlite3.connect(":memory:")) as connection:
                    if predecessor is not None:
                        connection.executescript(predecessor[2])
                    statements = []
                    buffer = ""
                    for char in contract["schema"]:
                        buffer += char
                        if char == ";" and sqlite3.complete_statement(buffer):
                            statements.append(buffer)
                            buffer = ""
                    for statement in statements:
                        connection.execute(statement)
                        connection.commit()
                        result = self.classify(connection)
                        self.assertIn(result.state, ("supported", "intermediate"))
                        connection.execute("PRAGMA query_only=OFF")
                        checkpoints += 1
                    for table, columns in contract["optional_columns"].items():
                        present = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
                        for name, definition in columns:
                            if name in present:
                                continue
                            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                            connection.commit()
                            self.assertIn(self.classify(connection).state, ("supported", "intermediate"))
                            connection.execute("PRAGMA query_only=OFF")
                            checkpoints += 1
                    for statement in contract["identity_indexes"]:
                        connection.execute(statement)
                        connection.commit()
                        self.assertIn(self.classify(connection).state, ("supported", "intermediate"))
                        connection.execute("PRAGMA query_only=OFF")
                        checkpoints += 1
                    self.assertEqual(self.classify(connection).state, "supported")
        self.assertGreater(checkpoints, 100)

    def test_released_rows_survive_current_startup_twice(self):
        from history_service.store import HistoryStore
        for ref, _commit, sql in RELEASE_SCHEMAS:
            with self.subTest(ref=ref), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "synthetic.sqlite3"
                with closing(sqlite3.connect(path)) as connection:
                    connection.executescript(sql)
                    connection.execute(
                        "INSERT INTO metric_samples (id, observed_at, system_id, enclosure_key, "
                        "slot, slot_label, metric_name, value_integer, serial) "
                        "VALUES (42, '2026-01-01T00:00:00+00:00', 'synthetic', 'shelf', "
                        "1, '1', 'temperature_c', 31, 'SYNTHETIC')"
                    )
                    connection.commit()
                    self.assertEqual(self.classify(connection).state, "supported")
                for _ in range(2):
                    HistoryStore(str(path), recover_unreadable_database=False)
                    with closing(sqlite3.connect(path)) as connection:
                        self.assertEqual(connection.execute(
                            "SELECT id, value_integer, serial FROM metric_samples"
                        ).fetchall(), [(42, 31, "SYNTHETIC")])
                        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        self.assertEqual(self.classify(connection).state, "supported")

    def test_malformed_database_remains_a_sqlite_fault(self):
        classify = self.classifier()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.sqlite3"
            path.write_bytes(b"not a database")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                with self.assertRaises(sqlite3.DatabaseError):
                    classify(connection)
            self.assertEqual(path.read_bytes(), b"not a database")

    def test_connection_must_already_be_query_only(self):
        classify = self.classifier()
        with closing(sqlite3.connect(":memory:")) as connection:
            with self.assertRaisesRegex(ValueError, "query-only"):
                classify(connection)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 0)

    def test_conflicting_definitions_are_not_repaired(self):
        base = RELEASE_SCHEMAS[-1][2]
        cases = [
            base.replace("slot INTEGER NOT NULL", "slot TEXT NOT NULL", 1),
            base.replace("details_json TEXT NOT NULL", "details_json TEXT", 1),
            base.replace("row_count >= 0", "row_count >= -1"),
            base.replace("row_count + 1", "row_count + 2", 1),
            base + "CREATE TABLE unexpected (value TEXT);",
            base + "CREATE VIEW unexpected AS SELECT 1;",
            base + "DROP TABLE slot_state_current; CREATE VIEW slot_state_current AS SELECT 1;",
            base + "DROP TABLE slot_state_current;",
            base.replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id INTEGER PRIMARY KEY", 1),
        ]
        classify = self.classifier()
        from history_service.schema_compatibility import SchemaIncompatibility
        for sql in cases:
            with self.subTest(sql=cases.index(sql)):
                with closing(sqlite3.connect(":memory:")) as connection:
                    connection.executescript(sql)
                    before = list(connection.iterdump())
                    connection.execute("PRAGMA query_only=ON")
                    with self.assertRaises(SchemaIncompatibility):
                        classify(connection)
                    self.assertEqual(before, list(connection.iterdump()))

    def test_classifier_does_not_execute_ddl_or_read_application_rows(self):
        classify = self.classifier()
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript(RELEASE_SCHEMAS[-1][2])
            connection.execute("PRAGMA query_only=ON")
            forbidden = []

            def authorize(action, arg1, arg2, database, source):
                if action == sqlite3.SQLITE_READ and arg1 != "sqlite_master":
                    forbidden.append((action, arg1))
                    return sqlite3.SQLITE_DENY
                if action not in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_PRAGMA):
                    forbidden.append((action, arg1))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            self.assertEqual(classify(connection).state, "supported")
            self.assertEqual(forbidden, [])

    def test_wal_committed_schema_is_visible_to_readonly_classifier(self):
        classify = self.classifier()
        from history_service.schema_compatibility import SchemaIncompatibility
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wal.sqlite3"
            with closing(sqlite3.connect(path)) as writer:
                writer.executescript(RELEASE_SCHEMAS[-1][2])
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE committed_wal_only (value TEXT)")
                writer.commit()
                # Ordinary read-only SQLite must observe committed WAL DDL.
                # Opening/closing a WAL reader can alter SHM lock bytes. That
                # admission-layer concern is explicitly outside this classifier.
                before = path.read_bytes(), Path(str(path) + "-wal").read_bytes()
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
                    reader.execute("PRAGMA query_only=ON")
                    with self.assertRaises(SchemaIncompatibility):
                        classify(reader)
                self.assertEqual(before, (path.read_bytes(), Path(str(path) + "-wal").read_bytes()))


class StartupAdmissionTests(unittest.TestCase):
    def snapshot(self, path):
        return {p.name: (p.read_bytes(), p.stat().st_mode)
                for p in path.parent.iterdir() if p.is_file()}

    def test_startup_and_lazy_connections_refuse_without_byte_or_mode_changes(self):
        from history_service.store import HistoryStore
        from history_service.schema_compatibility import SchemaIncompatibility
        cases = [("CREATE TABLE foreign_data(value TEXT);", 0),
                 ("CREATE TABLE foreign_data(value TEXT);", 1)]
        for marker in (-1, 2, 999):
            cases.extend([("", marker), (RELEASE_SCHEMAS[-1][2], marker)])
        cases.extend([(RELEASE_SCHEMAS[-1][2].replace("row_count + 1", "row_count + 2", 1), 1),
                      (RELEASE_SCHEMAS[-1][2] + "CREATE VIEW unknown_view AS SELECT 1;", 1)])
        for sql, marker in cases:
            for initialize in (True, False):
                with self.subTest(marker=marker, initialize=initialize, sql=sql[:30]), tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "synthetic.db"
                    with closing(sqlite3.connect(path)) as connection:
                        connection.executescript(sql)
                        connection.execute(f"PRAGMA user_version={marker}")
                        connection.commit()
                    path.chmod(0o666)
                    path.parent.chmod(0o777)
                    before = self.snapshot(path)
                    parent_mode = path.parent.stat().st_mode
                    with self.assertRaises(SchemaIncompatibility):
                        store = HistoryStore(str(path), initialize=initialize, permission_repair_enabled=True)
                        if not initialize:
                            with closing(store._connect()):
                                pass
                    self.assertEqual(self.snapshot(path), before)
                    self.assertEqual(path.parent.stat().st_mode, parent_mode)

    def test_wal_only_foreign_schema_is_refused_without_checkpoint(self):
        from history_service.store import HistoryStore
        from history_service.schema_compatibility import SchemaIncompatibility
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            with closing(sqlite3.connect(path)) as writer:
                writer.executescript(RELEASE_SCHEMAS[-1][2])
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE foreign_wal(value TEXT)")
                writer.commit()
                before = path.read_bytes(), Path(str(path) + "-wal").read_bytes()
                with self.assertRaises(SchemaIncompatibility):
                    HistoryStore(str(path))
                self.assertEqual(before, (path.read_bytes(), Path(str(path) + "-wal").read_bytes()))

    def test_reconnect_does_not_trust_cached_admission(self):
        from history_service.store import HistoryStore
        from history_service.schema_compatibility import SchemaIncompatibility
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            store = HistoryStore(str(path))
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA user_version=999")
            before = self.snapshot(path)
            with self.assertRaises(SchemaIncompatibility):
                with closing(store._connect()):
                    pass
            self.assertEqual(self.snapshot(path), before)

    def test_connection_retains_lifecycle_lock_until_close(self):
        from history_service.store import HistoryStore, history_write_lock
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            store = HistoryStore(str(path))
            connection = store._connect()
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "migration lock"):
                    with history_write_lock(path, blocking=False):
                        pass
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            finally:
                connection.close()
            with history_write_lock(path, blocking=False):
                pass

    def test_replacement_after_classification_is_refused_before_wal_or_permissions(self):
        import os
        from unittest.mock import patch
        from history_service import store as module
        from history_service.schema_compatibility import classify_schema
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            other = Path(temp) / "replacement.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(RELEASE_SCHEMAS[-1][2])
            with closing(sqlite3.connect(other)) as connection:
                connection.execute("CREATE TABLE foreign_data(value TEXT)")
            before = other.read_bytes()
            def replace_after_check(connection):
                result = classify_schema(connection)
                os.replace(other, path)
                return result
            with patch.object(module, "classify_schema", side_effect=replace_after_check, create=True):
                with self.assertRaisesRegex(ValueError, "identity"):
                    module.HistoryStore(str(path))
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path) + "-wal").exists())

    def test_connection_and_cursor_refuse_uncoordinated_replacement(self):
        import os
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            store = HistoryStore(str(path))
            with closing(store._connect()) as connection:
                cursor = connection.cursor()
                other = Path(temp) / "replacement.db"
                with closing(sqlite3.connect(other)) as replacement:
                    replacement.execute("CREATE TABLE foreign_data(value TEXT)")
                before = other.read_bytes()
                os.replace(other, path)
                for operation in (lambda: connection.execute("SELECT 1"),
                                  lambda: cursor.execute("SELECT 1"), connection.commit):
                    with self.assertRaisesRegex(ValueError, "identity"):
                        operation()
                self.assertEqual(path.read_bytes(), before)


    def test_wal_orphan_refusal_preserves_main_and_wal_bytes(self):
        import subprocess
        import sys
        from history_service.store import HistoryStore
        from history_service.schema_compatibility import SchemaIncompatibility
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            script = """import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE foreign_data(value TEXT)')
c.commit()
os._exit(0)
"""
            subprocess.run([sys.executable, "-c", script, str(path)], check=True)
            # Model a restart without the disposable shared-memory index.
            Path(str(path) + "-shm").unlink(missing_ok=True)
            before = path.read_bytes(), Path(str(path) + "-wal").read_bytes()
            with self.assertRaises(SchemaIncompatibility):
                HistoryStore(str(path))
            self.assertEqual(before, (path.read_bytes(), Path(str(path) + "-wal").read_bytes()))

    def test_rw_open_replacement_is_refused(self):
        import os
        from unittest.mock import patch
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            other = Path(temp) / "replacement.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(RELEASE_SCHEMAS[-1][2])
            with closing(sqlite3.connect(other)) as connection:
                connection.execute("CREATE TABLE foreign_data(value TEXT)")
            before = other.read_bytes()
            real_connect = sqlite3.connect
            def replace_at_open(target, **kwargs):
                connection = real_connect(target, **kwargs)
                if target == path:
                    os.replace(other, path)
                return connection
            with patch("history_service.store.sqlite3.connect", side_effect=replace_at_open):
                with self.assertRaisesRegex(ValueError, "identity"):
                    HistoryStore(str(path))
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path) + "-wal").exists())

    def test_actual_writer_is_reclassified_and_closed_before_read_guard_on_refusal(self):
        from unittest.mock import patch
        from history_service.store import HistoryStore
        from history_service.schema_compatibility import classify_schema, SchemaIncompatibility
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            HistoryStore(str(path))
            seen = []
            closed = []
            real_connect = sqlite3.connect
            class Tracking:
                def __init__(self, connection, kind):
                    self.connection = connection
                    self.kind = kind
                def __getattr__(self, name):
                    return getattr(self.connection, name)
                def close(self):
                    closed.append(self.kind)
                    self.connection.close()
            def connect(target, **kwargs):
                if target == ":memory:":
                    return real_connect(target, **kwargs)
                return Tracking(real_connect(target, **kwargs), "reader" if kwargs.get("uri") else "writer")
            def classify(connection):
                seen.append(connection.kind)
                result = classify_schema(connection)
                if connection.kind == "writer":
                    raise SchemaIncompatibility("backfill_marker")
                return result
            with patch("history_service.store.sqlite3.connect", side_effect=connect), patch(
                "history_service.store.classify_schema", side_effect=classify
            ):
                with self.assertRaises(SchemaIncompatibility):
                    HistoryStore(str(path))
            self.assertEqual(seen, ["reader", "writer"])
            self.assertEqual(closed, ["writer", "reader"])

    def test_aliases_and_unsafe_sidecars_refuse_without_touching_source(self):
        import os
        from history_service.store import HistoryStore
        for kind in ("hardlink", "symlink", "wal_symlink", "shm_hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / "synthetic.db"
                with closing(sqlite3.connect(path)) as connection:
                    connection.executescript(RELEASE_SCHEMAS[-1][2])
                original = path.read_bytes()
                other = root / "other"
                if kind == "hardlink":
                    os.link(path, other)
                elif kind == "symlink":
                    path.rename(other)
                    path.symlink_to(other)
                else:
                    other.write_bytes(b"synthetic sidecar sentinel")
                    sidecar = Path(str(path) + ("-wal" if kind == "wal_symlink" else "-shm"))
                    if kind == "wal_symlink":
                        sidecar.symlink_to(other)
                    else:
                        os.link(other, sidecar)
                with self.assertRaises(ValueError):
                    HistoryStore(str(path))
                self.assertEqual(path.read_bytes(), original)

    def test_existing_sidecar_replacement_after_admission_is_refused(self):
        import os
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            store = HistoryStore(str(path))
            with closing(store._connect()) as connection:
                wal = Path(str(path) + "-wal")
                other = Path(temp) / "other"
                other.write_bytes(wal.read_bytes())
                os.replace(other, wal)
                with self.assertRaisesRegex(ValueError, "identity"):
                    connection.execute("SELECT 1")

    def test_cursor_connection_does_not_escape_identity_guard(self):
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(str(Path(temp) / "synthetic.db"))
            with closing(store._connect()) as connection:
                self.assertIs(connection.cursor().connection, connection)


    def test_readonly_admission_fault_cannot_trigger_unclassified_mode_repair(self):
        from unittest.mock import patch
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE foreign_data(value TEXT)")
            path.chmod(0o666)
            before = self.snapshot(path)
            with patch("history_service.store.classify_schema", side_effect=sqlite3.OperationalError(
                "attempt to write a readonly database"
            )), patch.object(HistoryStore, "_normalize_database_permissions") as normalize:
                with self.assertRaises(sqlite3.OperationalError):
                    HistoryStore(str(path), permission_repair_enabled=True)
                normalize.assert_not_called()
            self.assertEqual(self.snapshot(path), before)


class PausedRecoveryTests(unittest.TestCase):
    def test_corruption_pauses_instead_of_empty_healthy_history(self):
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            original = b"synthetic corrupt sqlite"
            path.write_bytes(original)
            inode = path.stat().st_ino
            store = HistoryStore(str(path))
            with self.assertRaisesRegex(RuntimeError, "recovery required"):
                store.counts()
            self.assertFalse(path.exists())
            retained = path.with_name(path.name + ".recovery-required") / "main"
            self.assertEqual(retained.read_bytes(), original)
            self.assertEqual(retained.stat().st_ino, inode)
            for _ in range(2):
                restarted = HistoryStore(str(path))
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    restarted.counts()
                self.assertFalse(path.exists())

    def test_existing_intent_blocks_before_sqlite_even_with_readable_replacement(self):
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            HistoryStore(str(path))
            before = path.read_bytes()
            marker = path.with_name(path.name + ".recovery-required")
            marker.mkdir(mode=0o700)
            with patch("history_service.store.sqlite3.connect", side_effect=AssertionError("opened SQLite")):
                store = HistoryStore(str(path))
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    store.counts()
            self.assertEqual(path.read_bytes(), before)

    def test_paused_collector_does_not_start_or_fetch(self):
        import asyncio
        from history_service.store import HistoryStore
        from history_service.collector import HistoryCollector
        from history_service.config import HistorySettings
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            path.write_bytes(b"synthetic corrupt sqlite")
            collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), HistoryStore(str(path)))
            async def probe():
                await collector.start()
                try:
                    self.assertIsNone(collector._task)
                    with self.assertRaisesRegex(RuntimeError, "recovery required"):
                        await collector.run_once()
                finally:
                    await collector.stop()
            asyncio.run(probe())

    def test_paused_routes_keep_liveness_and_explicit_nonready_status(self):
        import asyncio
        import json
        from history_service import main
        from history_service.store import HistoryStore
        from history_service.collector import HistoryCollector
        from history_service.config import HistorySettings
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            path.write_bytes(b"synthetic corrupt sqlite")
            store = HistoryStore(str(path))
            collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), store)
            with patch.object(main, "store", store), patch.object(main, "collector", collector):
                self.assertEqual(asyncio.run(main.livez()).status_code, 200)
                health = asyncio.run(main.healthz())
                self.assertEqual(health.status_code, 503)
                payload = json.loads(health.body)
                self.assertTrue(payload["recovery_required"])
                self.assertNotIn(str(path), health.body.decode())
                self.assertNotIn("counts", payload)


    def test_fault_matrix_preserves_artifacts_and_pauses_fresh_processes(self):
        import json
        import os
        import subprocess
        import sys
        from history_service import recovery_state as recovery
        from history_service.migration_lock import history_write_lock
        from history_service.store import HistoryStore
        payloads = {role: ("synthetic-" + role).encode() for role in recovery.ARTIFACTS}
        # Count actual durability/publication boundaries rather than guessing.
        counts = {"fsync": 0, "link": 0, "unlink": 0}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            for role, suffix in recovery.ARTIFACTS.items():
                Path(str(path) + suffix).write_bytes(payloads[role])
            def counted(name, original):
                def call(*args, **kwargs):
                    counts[name] += 1
                    return original(*args, **kwargs)
                return call
            with patch.object(os, "fsync", counted("fsync", os.fsync)), patch.object(os, "link", counted("link", os.link)), patch.object(os, "unlink", counted("unlink", os.unlink)):
                with history_write_lock(path, blocking=False):
                    recovery.pause_and_quarantine(path)
        self.assertEqual(counts["link"], 4)
        self.assertEqual(counts["unlink"], 4)
        script = """
import os, sys
from pathlib import Path
from history_service.recovery_state import pause_and_quarantine
from history_service.migration_lock import history_write_lock
path, operation, index, timing = sys.argv[1:]
original = getattr(os, operation)
calls = 0
def fault(*args, **kwargs):
    global calls
    calls += 1
    if calls == int(index) and timing == 'before': os._exit(71)
    result = original(*args, **kwargs)
    if calls == int(index) and timing == 'after': os._exit(71)
    return result
setattr(os, operation, fault)
with history_write_lock(Path(path), blocking=False):
    pause_and_quarantine(Path(path))
"""
        restart = """
import json, sys
from history_service.store import HistoryStore
s = HistoryStore(sys.argv[1])
print(json.dumps(s.recovery_status()))
try: s.counts()
except RuntimeError: pass
else: raise AssertionError('healthy fallback')
"""
        for operation, count in counts.items():
            for index in range(1, count + 1):
                for timing in ("before", "after"):
                    with self.subTest(operation=operation, index=index, timing=timing), tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "history.db"
                        identities = {}
                        for role, suffix in recovery.ARTIFACTS.items():
                            artifact = Path(str(path) + suffix)
                            artifact.write_bytes(payloads[role])
                            identities[role] = artifact.stat().st_ino
                        result = subprocess.run([sys.executable, "-B", "-c", script, str(path), operation, str(index), timing], capture_output=True, text=True, timeout=20)
                        self.assertEqual(result.returncode, 71, result.stderr)
                        for role, suffix in recovery.ARTIFACTS.items():
                            candidates = [Path(str(path) + suffix), recovery.recovery_path(path) / role]
                            retained = [candidate for candidate in candidates if candidate.exists()]
                            self.assertTrue(retained)
                            for candidate in retained:
                                self.assertEqual(candidate.read_bytes(), payloads[role])
                                self.assertEqual(candidate.stat().st_ino, identities[role])
                        for _ in range(2):
                            result = subprocess.run([sys.executable, "-B", "-c", restart, str(path)], capture_output=True, text=True, timeout=20)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertTrue(json.loads(result.stdout)["recovery_required"])
                        self.assertTrue(HistoryStore(str(path)).recovery_status()["recovery_required"])

    def test_intent_binds_all_bytes_and_is_private_bounded_and_not_cleared(self):
        import hashlib
        import json
        import stat
        from history_service import recovery_state as recovery
        from history_service.migration_lock import history_write_lock
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            original = b"synthetic-corrupt-main"
            path.write_bytes(original)
            with history_write_lock(path, blocking=False):
                recovery.pause_and_quarantine(path)
            root = recovery.recovery_path(path)
            intent = root / "intent.json"
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(intent.stat().st_mode), 0o600)
            self.assertLess(intent.stat().st_size, recovery.MAX_RECORD_BYTES)
            record = json.loads(intent.read_bytes())
            self.assertEqual(record["artifacts"]["main"]["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(recovery.inspect_recovery(path), "required")
            before = intent.read_bytes()
            store = HistoryStore(str(path))
            with self.assertRaisesRegex(RuntimeError, "recovery required"):
                store.restore_backup(root / "main")
            self.assertEqual(intent.read_bytes(), before)
            self.assertFalse(path.exists())
            # Neither an ordinary healthy replacement nor record disappearance is
            # authorization for the already-paused process to resume.
            path.write_bytes(b"")
            root.rename(root.with_name("retained-evidence"))
            self.assertTrue(store.recovery_status()["recovery_required"])

    def test_invalid_markers_never_open_sqlite_or_follow_special_files(self):
        import os
        from history_service import recovery_state as recovery
        from history_service.store import HistoryStore
        for kind in ("symlink", "file", "fifo", "oversize", "malformed", "duplicate", "unknown", "mode"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.db"
                root = recovery.recovery_path(path)
                outside = Path(directory) / "untouched"
                outside.write_bytes(b"synthetic sentinel")
                if kind == "symlink":
                    root.symlink_to(outside)
                elif kind == "file":
                    root.write_bytes(b"not a directory")
                else:
                    root.mkdir(mode=0o700)
                    record = root / "intent.json"
                    if kind == "fifo":
                        os.mkfifo(record, 0o600)
                    else:
                        record.write_bytes({"oversize": b"x" * 8193, "malformed": b"{", "duplicate": b'{"version":1,"version":1}', "unknown": b'{"version":999}', "mode": b"{}"}[kind])
                        record.chmod(0o644 if kind == "mode" else 0o600)
                with patch("history_service.store.sqlite3.connect", side_effect=AssertionError("SQLite must not open")):
                    store = HistoryStore(str(path))
                    self.assertEqual(store.recovery_status()["recovery_state"], "invalid")
                    with self.assertRaisesRegex(RuntimeError, "recovery required"):
                        store.counts()
                self.assertEqual(outside.read_bytes(), b"synthetic sentinel")
                self.assertFalse(path.exists())

    def test_collision_and_persistent_replacement_preserve_both_identities(self):
        import os
        from history_service import recovery_state as recovery
        from history_service.migration_lock import history_write_lock
        for kind in ("collision", "replacement"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.db"
                path.write_bytes(b"original synthetic bytes")
                inode = path.stat().st_ino
                link = os.link
                def changed(source, target, **kwargs):
                    if kind == "collision":
                        Path(target).write_bytes(b"unrelated destination")
                    else:
                        path.rename(path.with_name("retained-original"))
                        path.write_bytes(b"unrelated replacement")
                    return link(source, target, **kwargs)
                with history_write_lock(path, blocking=False), patch.object(os, "link", side_effect=changed):
                    with self.assertRaises((OSError, recovery.HistoryRecoveryRequired)):
                        recovery.pause_and_quarantine(path)
                original = path if kind == "collision" else path.with_name("retained-original")
                self.assertEqual(original.read_bytes(), b"original synthetic bytes")
                self.assertEqual(original.stat().st_ino, inode)
                self.assertNotEqual(recovery.inspect_recovery(path), "none")
                if kind == "collision":
                    self.assertEqual((recovery.recovery_path(path) / "main").read_bytes(), b"unrelated destination")
                else:
                    self.assertEqual(path.read_bytes(), b"unrelated replacement")

    def test_asgi_pause_boundary_blocks_queries_and_mutation_but_not_liveness(self):
        import asyncio
        import json
        from history_service import main
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            path.write_bytes(b"synthetic corrupt SQLite")
            store = HistoryStore(str(path))
            async def request(method, route):
                messages = []
                requested = False
                async def receive():
                    nonlocal requested
                    if not requested:
                        requested = True
                        return {"type": "http.request", "body": b"", "more_body": False}
                    await asyncio.Event().wait()
                async def send(message):
                    messages.append(message)
                scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                         "http_version": "1.1", "method": method, "scheme": "http", "path": route,
                         "raw_path": route.encode(), "query_string": b"", "root_path": "",
                         "headers": [], "client": ("synthetic.test", 1), "server": ("synthetic.test", 80)}
                await main.app(scope, receive, send)
                status = next(m["status"] for m in messages if m["type"] == "http.response.start")
                raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
                return status, json.loads(raw), raw.decode()
            async def probe():
                for method, route in (("GET", "/"), ("GET", "/api/history/overview"), ("POST", "/api/history/refresh")):
                    status, payload, raw = await request(method, route)
                    self.assertEqual(status, 503)
                    self.assertTrue(payload["recovery_required"])
                    self.assertNotIn(str(path), raw)
                self.assertEqual((await request("GET", "/livez"))[0], 200)
                self.assertEqual((await request("GET", "/healthz"))[0], 503)
                self.assertTrue((await request("GET", "/api/history/recovery-status"))[1]["recovery_required"])
            with patch.object(main, "store", store):
                asyncio.run(probe())


    def test_sync_and_reservation_errors_preserve_source_and_observable_pause(self):
        from history_service import recovery_state as recovery
        from history_service.store import HistoryStore
        for boundary in ("reservation", "directory-sync", "record-sync"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.db"
                path.write_bytes(b"synthetic corrupt SQLite")
                inode = path.stat().st_ino
                if boundary == "reservation":
                    original = Path.mkdir
                    def denied(candidate, *args, **kwargs):
                        if candidate == recovery.recovery_path(path):
                            raise PermissionError("synthetic private error")
                        return original(candidate, *args, **kwargs)
                    injection = patch.object(Path, "mkdir", denied)
                elif boundary == "directory-sync":
                    injection = patch.object(recovery, "_sync_directory", side_effect=OSError("synthetic sync error"))
                else:
                    import os
                    original = os.fsync
                    calls = 0
                    def denied_sync(descriptor):
                        nonlocal calls
                        calls += 1
                        if calls == 3:
                            raise OSError("synthetic intent sync error")
                        return original(descriptor)
                    injection = patch.object(os, "fsync", denied_sync)
                with injection:
                    store = HistoryStore(str(path))
                self.assertTrue(store.recovery_status()["recovery_required"])
                self.assertEqual(path.read_bytes(), b"synthetic corrupt SQLite")
                self.assertEqual(path.stat().st_ino, inode)
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    store.counts()
                self.assertTrue(HistoryStore(str(path)).recovery_status()["recovery_required"])

    def test_well_formed_but_unsupported_intent_fields_fail_closed(self):
        import copy
        import json
        import os
        from history_service import recovery_state as recovery
        from history_service.store import HistoryStore
        valid = {"version": 1, "id": "a" * 32, "phase": "intent", "policy": "pause", "artifacts": {
            "main": {"dev": 1, "ino": 2, "size": 1, "mtime_ns": 1, "sha256": "b" * 64}}}
        cases = []
        for key, value in (("version", 999), ("version", True), ("id", "../unsafe"),
                           ("phase", "cleared"), ("policy", "continue"), ("artifacts", {}),
                           ("extra", "private-marker")):
            changed = copy.deepcopy(valid)
            changed[key] = value
            cases.append(changed)
        for key, value in (("ino", True), ("size", -1), ("sha256", "private-marker")):
            changed = copy.deepcopy(valid)
            changed["artifacts"]["main"][key] = value
            cases.append(changed)
        for index, record in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.db"
                root = recovery.recovery_path(path)
                root.mkdir(mode=0o700)
                intent = root / "intent.json"
                intent.write_text(json.dumps(record))
                intent.chmod(0o600)
                before = intent.read_bytes()
                store = HistoryStore(str(path))
                self.assertEqual(store.recovery_status()["recovery_state"], "invalid")
                self.assertEqual(intent.read_bytes(), before)
                self.assertFalse(path.exists())
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.db"
                root = recovery.recovery_path(path)
                root.mkdir(mode=0o700)
                target = Path(directory) / "untouched.json"
                target.write_text(json.dumps(valid))
                target.chmod(0o600)
                if kind == "symlink":
                    (root / "intent.json").symlink_to(target)
                else:
                    os.link(target, root / "intent.json")
                self.assertEqual(HistoryStore(str(path)).recovery_status()["recovery_state"], "invalid")
                self.assertEqual(json.loads(target.read_text()), valid)
