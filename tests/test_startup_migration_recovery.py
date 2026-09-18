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
import tempfile
import unittest
from pathlib import Path

from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    activation_pending_path,
)
from history_service.startup_migration import (
    ACTIVATION_PENDING_REASON,
    MIGRATION_RECOVERY_FAILED_REASON,
    pending_migration_marker_path,
    recover_pending_history_migration,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


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

    def test_startup_recovers_a_pending_migration_before_opening_the_store(self) -> None:
        factory_source = self._factory_source()

        self.assertIn("recover_pending_history_migration", factory_source)
        self.assertIn("HistoryStartupError", factory_source)
        self.assertLess(
            factory_source.index("recover_pending_history_migration"),
            factory_source.index("build_history_store"),
        )

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
