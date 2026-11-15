"""A pending segmented-history activation must not crash the history service.

An interrupted rotation or restore leaves an activation marker beside the hot
database. The store refuses to open while it is there, which is correct: the
interrupted operation has its own recovery and nothing may write to the
database first. Startup has to turn that refusal into the operator line that
`/healthz` reports, so the process stays up with history stopped instead of
failing at import and restarting forever.

These tests use a real `HistoryStore` in a temporary directory and the real
startup helpers. The service-level test imports `history_service.main` in a
separate interpreter configured for that directory only. Nothing here recovers
the interrupted operation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    activation_pending_path,
)
from history_service.startup import (
    HistorySchemaVersionError,
    HistoryStartupError,
    open_history_store_with_retries,
    recorded_startup_failure,
)
from history_service.startup_migration import (
    ACTIVATION_PENDING_REASON,
    open_history_store_after_recovery,
)
from history_service.store import CURRENT_SCHEMA_VERSION, HistoryStore

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTIVATION_MARKER_BYTES = b'{"status": "activation-pending"}'


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under `root` with its bytes (None for directories)."""
    entries: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        entries[relative] = None if path.is_dir() else path.read_bytes()
    return entries


class PendingActivationStartupTests(unittest.TestCase):
    def _layout(self, temp_dir: str) -> tuple[Path, Path, Path]:
        root = Path(temp_dir)
        sqlite_path = root / "history.db"
        catalog_path = root / "segments" / "catalog.json"
        catalog_path.parent.mkdir()
        # A real, current database, opened and closed once without a marker.
        HistoryStore(str(sqlite_path), segment_catalog_path=catalog_path)
        return root, sqlite_path, catalog_path

    def _write_activation_marker(self, sqlite_path: Path) -> Path:
        marker = activation_pending_path(sqlite_path)
        marker.write_bytes(ACTIVATION_MARKER_BYTES)
        return marker

    def _open(
        self,
        root: Path,
        sqlite_path: Path,
        catalog_path: Path,
        *,
        build_store=None,
        recover=None,
        recoveries: list[str],
    ):
        return open_history_store_with_retries(
            lambda: open_history_store_after_recovery(
                sqlite_path=sqlite_path,
                segment_catalog_path=catalog_path,
                build_store=build_store
                or (lambda: HistoryStore(str(sqlite_path), segment_catalog_path=catalog_path)),
                recover=recover or (lambda source, segments: recoveries.append("recover")),
            ),
            directory=root,
            attempts=1,
            initial_backoff_seconds=0.0,
            sleep=lambda _seconds: None,
        )

    def test_a_pending_activation_becomes_the_startup_reason_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, sqlite_path, catalog_path = self._layout(temp_dir)
            marker = self._write_activation_marker(sqlite_path)
            before = _tree(root)
            recoveries: list[str] = []

            with self.assertLogs("history_service", level="ERROR"):
                with self.assertRaises(HistoryStartupError) as raised:
                    self._open(root, sqlite_path, catalog_path, recoveries=recoveries)

            self.assertEqual(raised.exception.reason, ACTIVATION_PENDING_REASON)
            # The store's own refusal is what was translated, nothing broader.
            self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
            self.assertIn("activation is pending", str(raised.exception.__cause__))
            self.assertEqual(recorded_startup_failure(), ACTIVATION_PENDING_REASON)
            self.assertEqual(recoveries, [], "no recovery may run for a rotation or restore")
            self.assertEqual(marker.read_bytes(), ACTIVATION_MARKER_BYTES)
            self.assertEqual(_tree(root), before, "startup must not write while activation is pending")

    def test_with_both_markers_the_activation_wins_and_migration_recovery_does_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, sqlite_path, catalog_path = self._layout(temp_dir)
            self._write_activation_marker(sqlite_path)
            migration_marker = catalog_path.parent / MIGRATION_PENDING_MARKER
            migration_marker.write_text(json.dumps({"status": "migration-pending"}), encoding="utf-8")
            before = _tree(root)
            recoveries: list[str] = []

            with self.assertLogs("history_service", level="ERROR"):
                with self.assertRaises(HistoryStartupError) as raised:
                    self._open(root, sqlite_path, catalog_path, recoveries=recoveries)

            self.assertEqual(raised.exception.reason, ACTIVATION_PENDING_REASON)
            self.assertEqual(recoveries, [])
            self.assertEqual(_tree(root), before)

    def test_a_marker_that_appears_after_the_first_check_is_reported_the_same_way(self) -> None:
        # No marker when startup looks; a rotation or restore publishes one
        # before the store's own admission runs. The real store refuses it and
        # startup must still report the activation reason, not crash.
        with tempfile.TemporaryDirectory() as temp_dir:
            root, sqlite_path, catalog_path = self._layout(temp_dir)
            database_before = sqlite_path.read_bytes()
            before = _tree(root)
            recoveries: list[str] = []
            marker = activation_pending_path(sqlite_path)

            def build_store():
                if not marker.exists():
                    marker.write_bytes(ACTIVATION_MARKER_BYTES)
                return HistoryStore(str(sqlite_path), segment_catalog_path=catalog_path)

            with self.assertLogs("history_service", level="ERROR"):
                with self.assertRaises(HistoryStartupError) as raised:
                    self._open(
                        root, sqlite_path, catalog_path, build_store=build_store, recoveries=recoveries,
                    )

            self.assertEqual(raised.exception.reason, ACTIVATION_PENDING_REASON)
            self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
            self.assertEqual(recoveries, [])
            self.assertEqual(sqlite_path.read_bytes(), database_before)
            self.assertEqual(marker.read_bytes(), ACTIVATION_MARKER_BYTES)
            self.assertEqual(
                _tree(root),
                {**before, marker.name: ACTIVATION_MARKER_BYTES},
                "only the marker the test published may be new",
            )

    def test_a_marker_that_appears_during_migration_recovery_is_reported_the_same_way(self) -> None:
        # The store refuses a pending migration, recovery runs once, and by the
        # reopen an activation marker is pending. The reopen must report it.
        with tempfile.TemporaryDirectory() as temp_dir:
            root, sqlite_path, catalog_path = self._layout(temp_dir)
            migration_marker = catalog_path.parent / MIGRATION_PENDING_MARKER
            migration_marker.write_text(json.dumps({"status": "migration-pending"}), encoding="utf-8")
            database_before = sqlite_path.read_bytes()
            marker = activation_pending_path(sqlite_path)
            recoveries: list[str] = []

            def recover(source: Path, segments: Path) -> dict[str, str]:
                recoveries.append("recover")
                migration_marker.unlink()
                marker.write_bytes(ACTIVATION_MARKER_BYTES)
                return {"recovery_state": "forward-completed"}

            with self.assertLogs("history_service", level="ERROR"):
                with self.assertRaises(HistoryStartupError) as raised:
                    self._open(
                        root, sqlite_path, catalog_path, recover=recover, recoveries=recoveries,
                    )

            self.assertEqual(raised.exception.reason, ACTIVATION_PENDING_REASON)
            self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
            self.assertEqual(recoveries, ["recover"], "recovery runs once, for the migration only")
            self.assertEqual(sqlite_path.read_bytes(), database_before)
            self.assertEqual(marker.read_bytes(), ACTIVATION_MARKER_BYTES)

    def test_control_without_a_marker_the_store_opens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, sqlite_path, catalog_path = self._layout(temp_dir)
            recoveries: list[str] = []

            opened = self._open(root, sqlite_path, catalog_path, recoveries=recoveries)

            self.assertIsInstance(opened, HistoryStore)
            self.assertIsNone(recorded_startup_failure())
            self.assertEqual(recoveries, [])

    def test_control_a_future_schema_database_is_still_refused_by_the_schema_gate(self) -> None:
        # Schema admission runs before the marker check and must keep its own
        # reason; the activation translation must not hide it.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sqlite_path = root / "history.db"
            catalog_path = root / "segments" / "catalog.json"
            catalog_path.parent.mkdir()
            connection = sqlite3.connect(sqlite_path)
            try:
                connection.execute("CREATE TABLE future_fixture (id INTEGER PRIMARY KEY)")
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 998}")
                connection.commit()
            finally:
                connection.close()
            self._write_activation_marker(sqlite_path)
            before = _tree(root)
            recoveries: list[str] = []

            with self.assertLogs("history_service", level="ERROR"):
                with self.assertRaises(HistorySchemaVersionError) as raised:
                    self._open(
                        root,
                        sqlite_path,
                        catalog_path,
                        build_store=lambda: HistoryStore(
                            str(sqlite_path),
                            segment_catalog_path=catalog_path,
                            recover_unreadable_database=False,
                        ),
                        recoveries=recoveries,
                    )

            self.assertNotEqual(raised.exception.reason, ACTIVATION_PENDING_REASON)
            self.assertEqual(recoveries, [])
            self.assertEqual(_tree(root), before)

    def test_control_other_operational_errors_with_a_marker_propagate_unchanged(self) -> None:
        # Lock contention and every other operational failure stay what they
        # are; only the store's explicit activation refusal is translated.
        with tempfile.TemporaryDirectory() as temp_dir:
            root, sqlite_path, catalog_path = self._layout(temp_dir)
            self._write_activation_marker(sqlite_path)
            recoveries: list[str] = []
            failure = sqlite3.OperationalError("database is locked")

            def build_store():
                raise failure

            with self.assertRaises(sqlite3.OperationalError) as raised:
                self._open(
                    root, sqlite_path, catalog_path, build_store=build_store, recoveries=recoveries,
                )

            self.assertIs(raised.exception, failure)
            self.assertEqual(recoveries, [])

    def test_control_activation_wording_without_a_marker_on_disk_is_not_translated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, sqlite_path, catalog_path = self._layout(temp_dir)
            recoveries: list[str] = []
            failure = sqlite3.OperationalError("segmented history activation is pending")

            def build_store():
                raise failure

            with self.assertRaises(sqlite3.OperationalError) as raised:
                self._open(
                    root, sqlite_path, catalog_path, build_store=build_store, recoveries=recoveries,
                )

            self.assertIs(raised.exception, failure)
            self.assertEqual(recoveries, [])


# Runs in a fresh interpreter whose environment points every history path at
# the test's temporary directory before `history_service.main` is imported, so
# its module-level startup never sees the ambient or default history path. An
# audit hook records every path the process opens for writing, creates,
# renames, removes or connects SQLite to; the parent asserts all of them are
# inside the temporary directory.
_RUNTIME_CHILD = r"""
import asyncio, json, os, sys

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
PATH_EVENTS = {
    "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.chmod", "os.chown",
    "os.truncate", "os.symlink", "os.link", "os.utime", "sqlite3.connect",
}
touched = []

def _resolve(path, dir_fd=None):
    if isinstance(path, int):
        try:
            return os.readlink(f"/proc/self/fd/{path}")
        except OSError:
            return f"<fd {path}>"
    path = os.fsdecode(path)
    if path == ":memory:" or path.startswith("file::memory:"):
        return None
    if not os.path.isabs(path):
        base = os.getcwd()
        if isinstance(dir_fd, int):
            try:
                base = os.readlink(f"/proc/self/fd/{dir_fd}")
            except OSError:
                return f"<dir_fd {dir_fd}>/{path}"
        path = os.path.join(base, path)
    return os.path.realpath(path)

def _hook(event, args):
    try:
        if event == "open":
            path, mode, flags = args
            if path is None:
                return
            writes = any(c in str(mode or "") for c in "wax+") or bool((flags or 0) & WRITE_FLAGS)
            if writes:
                resolved = _resolve(path)
                if resolved:
                    touched.append([event, resolved])
        elif event in PATH_EVENTS and args:
            dir_fd = args[-1] if event != "sqlite3.connect" and isinstance(args[-1], int) else None
            resolved = _resolve(args[0], dir_fd)
            if resolved:
                touched.append([event, resolved])
            if event == "os.rename":
                resolved = _resolve(args[1], args[3] if len(args) > 3 else None)
                if resolved:
                    touched.append([event, resolved])
    except Exception as exc:  # the hook must never hide a path
        touched.append([event, f"<unresolved {type(exc).__name__}>"])

sys.addaudithook(_hook)

from history_service import main  # noqa: E402  (module-level startup runs here)

async def _request(app, path):
    sent = []
    finished = asyncio.Event()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body.decode("utf-8")

async def _drive():
    app = main.app
    lifespan_in = asyncio.Queue()
    lifespan_out = []
    await lifespan_in.put({"type": "lifespan.startup"})

    async def send(message):
        lifespan_out.append(message["type"])

    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, lifespan_in.get, send)
    )
    for _ in range(200):
        if lifespan_out:
            break
        await asyncio.sleep(0.01)
    responses = {path: await _request(app, path) for path in ("/healthz", "/livez", "/api/history/overview")}
    await lifespan_in.put({"type": "lifespan.shutdown"})
    await asyncio.wait_for(task, 10)
    return lifespan_out, responses

lifespan, responses = asyncio.run(_drive())
print("RESULT " + json.dumps({
    "store_is_none": main.store is None,
    "collector_is_none": main.collector is None,
    "reason": main.startup_failure_reason,
    "unavailable_detail": main.HISTORY_UNAVAILABLE_DETAIL,
    "lifespan": lifespan,
    "responses": responses,
    "touched": touched,
}))
"""

# Startup infrastructure that settings loading and the SQLite temporary
# directory create beside the history database. Nothing else may appear.
_STARTUP_INFRASTRUCTURE = (".sqlite-tmp", "backups")


class PendingActivationRuntimeTests(unittest.TestCase):
    """The composed service startup, in isolation, stays up and reports the reason."""

    def test_the_service_stays_up_with_down_health_and_touches_only_its_own_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            history_dir = root / "history"
            history_dir.mkdir()
            sqlite_path = history_dir / "history.db"
            catalog_path = history_dir / "segments" / "catalog.json"
            catalog_path.parent.mkdir()
            child_tmp = root / "tmp"
            child_tmp.mkdir(mode=0o700)
            HistoryStore(str(sqlite_path), segment_catalog_path=catalog_path)
            marker = activation_pending_path(sqlite_path)
            marker.write_bytes(ACTIVATION_MARKER_BYTES)
            before = _tree(history_dir)

            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("HISTORY_", "RELEASE_CHECK_", "BACKUP_", "SCHEDULED_BACKUP_"))
                and key not in {"PYTHONPATH", "APP_CONFIG_PATH", "TMPDIR", "SQLITE_TMPDIR"}
            }
            env.update(
                {
                    "HISTORY_SQLITE_PATH": str(sqlite_path),
                    "HISTORY_SEGMENT_CATALOG_PATH": str(catalog_path),
                    "HISTORY_BACKUP_DIR": str(history_dir / "backups"),
                    "HISTORY_LONG_TERM_BACKUP_DIR": str(history_dir / "backups" / "long-term"),
                    "RELEASE_CHECK_ENABLED": "false",
                    "TMPDIR": str(child_tmp),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            # The repository is the working directory, so a relative or default
            # path would land in the checkout and fail the containment check.
            completed = subprocess.run(
                [sys.executable, "-c", _RUNTIME_CHILD],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
            lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
            self.assertEqual(len(lines), 1, completed.stdout[-4000:])
            result = json.loads(lines[0].removeprefix("RESULT "))

            # The process came up with history closed and the activation reason.
            self.assertTrue(result["store_is_none"], "history stays closed while activation is pending")
            self.assertTrue(result["collector_is_none"], "no collector, so no collection or backup work")
            self.assertEqual(result["reason"], ACTIVATION_PENDING_REASON)
            self.assertEqual(result["lifespan"], ["lifespan.startup.complete", "lifespan.shutdown.complete"])
            health_status, health_body = result["responses"]["/healthz"]
            self.assertEqual(health_status, 503)
            self.assertEqual(
                json.loads(health_body),
                {"status": "down", "detail": result["unavailable_detail"], "reason": ACTIVATION_PENDING_REASON},
            )
            self.assertEqual(result["responses"]["/livez"][0], 200)
            api_status, api_body = result["responses"]["/api/history/overview"]
            self.assertEqual(api_status, 503)
            self.assertEqual(json.loads(api_body), {"detail": result["unavailable_detail"]})

            # Isolation: every path the process wrote, created or opened with
            # SQLite is inside this test's directory, and the hook was live.
            touched = [path for _event, path in result["touched"]]
            self.assertTrue(
                any(path.startswith(str(history_dir) + os.sep) for path in touched),
                "the audit hook recorded no history-directory activity",
            )
            outside = [
                entry
                for entry in result["touched"]
                if not (entry[1] == str(root) or entry[1].startswith(str(root) + os.sep))
            ]
            self.assertEqual(outside, [], "startup touched a path outside its temporary directory")

            # The protected material is unchanged: the database and its
            # sidecars, the marker and the segment folder. Anything new is the
            # startup infrastructure named above.
            after = _tree(history_dir)
            for name, content in before.items():
                self.assertIn(name, after)
                self.assertEqual(after[name], content, f"{name} changed")
            new_entries = sorted(set(after) - set(before))
            self.assertEqual(
                [name for name in new_entries if name.split(os.sep, 1)[0] not in _STARTUP_INFRASTRUCTURE],
                [],
            )
            self.assertEqual(marker.read_bytes(), ACTIVATION_MARKER_BYTES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
