"""A pending segmented-history activation must not crash the history service.

An interrupted rotation or restore leaves an activation marker beside the hot
database. The store refuses to open while it is there, which is correct: the
interrupted operation has its own recovery and nothing may write to the
database first. Startup has to turn that refusal into the operator line that
`/healthz` reports, so the process stays up with history stopped instead of
failing at import and restarting forever.

These tests use a real `HistoryStore` in a temporary directory and the real
startup helpers. Nothing here recovers the interrupted operation.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        recoveries: list[str],
    ):
        return open_history_store_with_retries(
            lambda: open_history_store_after_recovery(
                sqlite_path=sqlite_path,
                segment_catalog_path=catalog_path,
                build_store=build_store
                or (lambda: HistoryStore(str(sqlite_path), segment_catalog_path=catalog_path)),
                recover=lambda source, segments: recoveries.append("recover"),
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


class PendingActivationRuntimeTests(unittest.TestCase):
    """The composed service startup stays up and reports the reason."""

    def test_the_runtime_stays_up_with_down_health_and_the_activation_reason(self) -> None:
        from history_service import main as history_main
        from history_service.config import HistorySettings

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sqlite_path = root / "history.db"
            catalog_path = root / "segments" / "catalog.json"
            catalog_path.parent.mkdir()
            HistoryStore(str(sqlite_path), segment_catalog_path=catalog_path)
            marker = activation_pending_path(sqlite_path)
            marker.write_bytes(ACTIVATION_MARKER_BYTES)
            loaded = HistorySettings(
                sqlite_path=str(sqlite_path),
                segment_catalog_path=str(catalog_path),
                release_check_enabled=False,
            )
            before = _tree(root)

            with (
                patch.object(history_main, "get_history_settings", return_value=loaded),
                # Process-wide SQLite setting; not what this test is about.
                patch.object(history_main, "configure_sqlite_temporary_directory"),
                self.assertLogs("history_service", level="ERROR"),
            ):
                settings, store, reason = history_main.load_history_runtime(
                    attempts=1,
                    initial_backoff_seconds=0.0,
                    sleep=lambda _seconds: None,
                )

            self.assertIsNone(store, "history stays closed while activation is pending")
            self.assertIsNotNone(settings)
            self.assertEqual(reason, ACTIVATION_PENDING_REASON)
            self.assertEqual(marker.read_bytes(), ACTIVATION_MARKER_BYTES)
            self.assertEqual(_tree(root), before)

            with (
                patch.object(history_main, "startup_failure_reason", reason),
                patch.object(history_main, "collector", None),
                patch.object(history_main, "store", None),
            ):
                response = asyncio.run(history_main.healthz())

            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                json.loads(response.body),
                {
                    "status": "down",
                    "detail": history_main.HISTORY_UNAVAILABLE_DETAIL,
                    "reason": ACTIVATION_PENDING_REASON,
                },
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
