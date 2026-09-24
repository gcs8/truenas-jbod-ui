"""An interrupted history migration must finish inside an ordinary startup.

The supported upgrade is a pin change plus `docker compose pull` and
`docker compose up -d`. A migration interrupted by a restart used to leave the
history container in a crash loop until an operator ran a recovery command by
hand, which is the partially applied manual repair a normal upgrade must never
require.

`history_service.main` cannot be imported on Windows (the collector imports
`fcntl`), so the wiring is asserted against the module source; the behaviour
itself is exercised directly and runs everywhere.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    activation_pending_path,
)
from history_service.startup import HistoryStartupError
from history_service.startup_migration import (
    ACTIVATION_PENDING_REASON,
    MIGRATION_RECOVERY_FAILED_REASON,
    open_history_store_after_recovery,
    pending_migration_marker_path,
    recover_pending_history_migration,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The on-disk schema-version gate lands separately (#546). These tests describe
# the composition of the two changes, so they are skipped until it is present
# rather than duplicating it here.
try:  # pragma: no cover - the branch taken depends on which change is applied
    from history_service.startup import HistorySchemaVersionError
except ImportError:  # pragma: no cover
    HistorySchemaVersionError = None

requires_schema_version_gate = unittest.skipIf(
    HistorySchemaVersionError is None,
    "The history schema-version admission gate is not present in this build.",
)


class PendingMigrationRecoveryTests(unittest.TestCase):
    def _layout(self, temp_dir: str) -> tuple[Path, Path]:
        root = Path(temp_dir)
        segments = root / "segments"
        segments.mkdir()
        sqlite_path = root / "history.db"
        sqlite_path.write_bytes(b"")
        return sqlite_path, segments / "catalog.json"

    def test_an_ordinary_startup_runs_no_migration_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            calls: list[tuple[Path, Path]] = []

            reason = recover_pending_history_migration(
                sqlite_path=sqlite_path,
                segment_catalog_path=catalog_path,
                recover=lambda source, segments: calls.append((source, segments)),
            )

            self.assertIsNone(reason)
            self.assertEqual(calls, [])

    def test_a_deployment_without_segments_is_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, _catalog_path = self._layout(temp_dir)

            self.assertIsNone(pending_migration_marker_path(None))
            self.assertIsNone(
                recover_pending_history_migration(
                    sqlite_path=sqlite_path,
                    segment_catalog_path=None,
                    recover=lambda source, segments: self.fail("recovery must not run"),
                )
            )

    def test_an_interrupted_migration_is_recovered_automatically_and_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            marker = catalog_path.parent / MIGRATION_PENDING_MARKER
            marker.write_text(json.dumps({"status": "migration-pending"}), encoding="utf-8")
            calls: list[tuple[Path, Path]] = []

            def recover(source: Path, segments: Path) -> dict[str, str]:
                calls.append((source, segments))
                marker.unlink()
                return {"recovery_state": "forward-completed"}

            reason = recover_pending_history_migration(
                sqlite_path=sqlite_path,
                segment_catalog_path=catalog_path,
                recover=recover,
            )

            self.assertIsNone(reason)
            self.assertEqual(calls, [(sqlite_path.absolute(), catalog_path.parent.absolute())])

            # Retrying the start after a successful recovery does no work again.
            reason = recover_pending_history_migration(
                sqlite_path=sqlite_path,
                segment_catalog_path=catalog_path,
                recover=recover,
            )
            self.assertIsNone(reason)
            self.assertEqual(len(calls), 1)

    def test_a_failed_recovery_fails_closed_with_one_concise_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            marker = catalog_path.parent / MIGRATION_PENDING_MARKER
            marker.write_text(json.dumps({"status": "migration-pending"}), encoding="utf-8")

            def recover(source: Path, segments: Path):
                raise ValueError("Segmented history migration recovery marker is invalid.")

            with self.assertLogs("history_service.startup_migration", level="ERROR"):
                reason = recover_pending_history_migration(
                    sqlite_path=sqlite_path,
                    segment_catalog_path=catalog_path,
                    recover=recover,
                )

            self.assertEqual(reason, MIGRATION_RECOVERY_FAILED_REASON)
            self.assertNotIn("\n", reason)
            self.assertNotIn("Traceback", reason)
            # The database is left exactly as recovery found it.
            self.assertTrue(marker.exists())

    def test_a_pending_activation_fails_closed_without_guessing_at_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            activation_pending_path(sqlite_path).write_text("{}", encoding="utf-8")

            with self.assertLogs("history_service.startup_migration", level="ERROR"):
                reason = recover_pending_history_migration(
                    sqlite_path=sqlite_path,
                    segment_catalog_path=catalog_path,
                    recover=lambda source, segments: self.fail("recovery must not run"),
                )

            self.assertEqual(reason, ACTIVATION_PENDING_REASON)
            self.assertNotIn("\n", reason)


class AdmissionBeforeRecoveryTests(unittest.TestCase):
    """Recovery is a write, so the store's admission has to run first.

    The schema-version gate refuses a database written by a newer release and
    leaves it byte-identical (#416). If startup recovery ran first, a pending
    migration marker would be recovered -- durable state mutated -- against a
    database this build had already been told not to touch.
    """

    def _layout(self, temp_dir: str) -> tuple[Path, Path]:
        root = Path(temp_dir)
        segments = root / "segments"
        segments.mkdir()
        sqlite_path = root / "history.db"
        return sqlite_path, segments / "catalog.json"

    def _pending_marker(self, catalog_path: Path) -> Path:
        marker = catalog_path.parent / MIGRATION_PENDING_MARKER
        marker.write_text(json.dumps({"status": "migration-pending"}), encoding="utf-8")
        return marker

    def test_an_ordinary_start_opens_the_store_without_consulting_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            sqlite_path.write_bytes(b"")

            opened = open_history_store_after_recovery(
                sqlite_path=sqlite_path,
                segment_catalog_path=catalog_path,
                build_store=lambda: "store",
                recover=lambda source, segments: self.fail("recovery must not run"),
            )

            self.assertEqual(opened, "store")

    def test_a_store_refusal_that_is_not_the_marker_is_raised_before_recovery(self) -> None:
        # The store decides admission. Anything it refuses other than the
        # pending marker itself must reach the operator untouched, with the
        # durable state exactly as it was found.
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            sqlite_path.write_bytes(b"durable-state")
            marker = self._pending_marker(catalog_path)
            attempts: list[str] = []

            def build_store() -> str:
                attempts.append("build")
                raise HistoryStartupError("This build must not write to this database.")

            with self.assertRaises(HistoryStartupError) as raised:
                open_history_store_after_recovery(
                    sqlite_path=sqlite_path,
                    segment_catalog_path=catalog_path,
                    build_store=build_store,
                    recover=lambda source, segments: self.fail("recovery must not run"),
                )

            self.assertEqual(attempts, ["build"])
            self.assertIn("must not write", raised.exception.reason)
            self.assertEqual(sqlite_path.read_bytes(), b"durable-state")
            self.assertTrue(marker.exists())

    def test_lock_contention_with_a_pending_marker_remains_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            marker = self._pending_marker(catalog_path)
            holder = sqlite3.connect(sqlite_path)
            holder.execute("CREATE TABLE lock_fixture (id INTEGER PRIMARY KEY)")
            holder.commit()
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO lock_fixture VALUES (1)")
            recoveries: list[str] = []

            def build_store() -> str:
                contender = sqlite3.connect(sqlite_path, timeout=0)
                try:
                    contender.execute("BEGIN IMMEDIATE")
                finally:
                    contender.close()
                return "store"

            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    open_history_store_after_recovery(
                        sqlite_path=sqlite_path,
                        segment_catalog_path=catalog_path,
                        build_store=build_store,
                        recover=lambda source, segments: recoveries.append("recover"),
                    )
            finally:
                holder.rollback()
                holder.close()

            self.assertEqual(recoveries, [])
            self.assertTrue(marker.exists())

    def test_the_pending_marker_refusal_is_still_recovered_and_the_store_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            sqlite_path.write_bytes(b"")
            marker = self._pending_marker(catalog_path)
            attempts: list[str] = []

            def build_store() -> str:
                attempts.append("build")
                if marker.exists():
                    raise sqlite3.OperationalError(
                        "Segmented history migration recovery is pending; refusing to open "
                        "the history database until the pending migration is recovered."
                    )
                return "store"

            def recover(source: Path, segments: Path) -> dict[str, str]:
                attempts.append("recover")
                marker.unlink()
                return {"recovery_state": "forward-completed"}

            with self.assertLogs("history_service.startup_migration", level="WARNING"):
                opened = open_history_store_after_recovery(
                    sqlite_path=sqlite_path,
                    segment_catalog_path=catalog_path,
                    build_store=build_store,
                    recover=recover,
                )

            self.assertEqual(opened, "store")
            self.assertEqual(attempts, ["build", "recover", "build"])

    def test_a_failed_recovery_still_fails_closed_with_one_concise_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            sqlite_path.write_bytes(b"")
            marker = self._pending_marker(catalog_path)

            def build_store() -> str:
                raise sqlite3.OperationalError("migration recovery is pending")

            def recover(source: Path, segments: Path):
                raise ValueError("Segmented history migration recovery marker is invalid.")

            with self.assertLogs("history_service.startup_migration", level="ERROR"):
                with self.assertRaises(HistoryStartupError) as raised:
                    open_history_store_after_recovery(
                        sqlite_path=sqlite_path,
                        segment_catalog_path=catalog_path,
                        build_store=build_store,
                        recover=recover,
                    )

            self.assertEqual(raised.exception.reason, MIGRATION_RECOVERY_FAILED_REASON)
            self.assertTrue(marker.exists())

    @requires_schema_version_gate
    def test_a_future_schema_database_with_a_pending_marker_is_refused_untouched(self) -> None:
        # The composed scenario: the schema gate and startup recovery both
        # apply, and the gate has to win.
        from history_service.store import CURRENT_SCHEMA_VERSION, HistoryStore

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path, catalog_path = self._layout(temp_dir)
            connection = sqlite3.connect(sqlite_path)
            try:
                connection.execute("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)")
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 998}")
                connection.commit()
            finally:
                connection.close()
            marker = self._pending_marker(catalog_path)
            database_before = sqlite_path.read_bytes()
            marker_before = marker.read_bytes()

            with self.assertRaises(HistorySchemaVersionError):
                open_history_store_after_recovery(
                    sqlite_path=sqlite_path,
                    segment_catalog_path=catalog_path,
                    build_store=lambda: HistoryStore(
                        str(sqlite_path),
                        segment_catalog_path=catalog_path,
                        recover_unreadable_database=False,
                    ),
                    recover=lambda source, segments: self.fail("recovery must not run"),
                )

            self.assertEqual(sqlite_path.read_bytes(), database_before)
            self.assertEqual(marker.read_bytes(), marker_before)


class HistoryStartupWiringTests(unittest.TestCase):
    """The recovery runs before the store opens, inside the guarded startup."""

    def _factory_source(self) -> str:
        source = (REPO_ROOT / "history_service" / "main.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        runtime = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "open_history_runtime"
        )
        factory = next(
            node
            for node in ast.walk(runtime)
            if isinstance(node, ast.FunctionDef) and node.name == "factory"
        )
        return ast.unparse(factory)

    def test_startup_opens_the_store_through_the_admission_first_helper(self) -> None:
        # The factory must not call recovery itself: the ordering contract
        # (admission, then recovery, then the store) lives in one place so a
        # future-schema database cannot be recovered before it is refused.
        factory_source = self._factory_source()

        self.assertIn("open_history_store_after_recovery", factory_source)
        self.assertIn("build_history_store", factory_source)
        self.assertNotIn("recover_pending_history_migration", factory_source)

    def test_the_container_image_needs_no_manual_migration_command_to_upgrade(self) -> None:
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        # The history service still starts with the plain application command.
        self.assertIn(
            '["uvicorn", "history_service.main:app", "--host", "0.0.0.0", "--port", "8001"]',
            compose,
        )
        self.assertNotIn("migrate_segmented_history", compose)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
