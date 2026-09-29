"""Damage found while running pauses collection, durably, until recovery (#417).

Every database here is synthetic and lives in a temporary directory. Nothing
in this module deletes or rewrites history rows, and the recovery CLI is
exercised against copies only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from history_service import main as history_main
from history_service import recovery
from history_service.collector import (
    COLLECTION_PAUSED_REASON,
    HistoryCollectionPaused,
    HistoryCollector,
)
from history_service.config import HistorySettings
from history_service.store import HistoryStore, is_database_corruption_error

requires_posix_migration_lock = unittest.skipUnless(
    importlib.util.find_spec("fcntl") is not None,
    "History migration locking requires POSIX flock support.",
)

MALFORMED = sqlite3.DatabaseError("database disk image is malformed")
NOT_A_DATABASE = sqlite3.DatabaseError("file is not a database")


def _digest_tree(directory: Path) -> dict[str, str]:
    """Bytes of every file that holds data.

    Any SQLite reader of a WAL database, even `mode=ro`, creates the `-shm`
    index and an empty `-wal`; neither carries data, so `-shm` is left out and
    a `-wal` counts only when it holds frames.
    """

    digests: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.endswith("-shm"):
            continue
        if path.name.endswith("-wal") and path.stat().st_size == 0:
            continue
        digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


class CorruptionClassifierTests(unittest.TestCase):
    def test_only_sqlite_damage_counts(self) -> None:
        self.assertTrue(is_database_corruption_error(MALFORMED))
        self.assertTrue(is_database_corruption_error(NOT_A_DATABASE))
        try:
            try:
                raise MALFORMED
            except sqlite3.DatabaseError as inner:
                raise RuntimeError("retention batch failed") from inner
        except RuntimeError as wrapped:
            self.assertTrue(is_database_corruption_error(wrapped))
        for ordinary in (
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("attempt to write a readonly database"),
            sqlite3.OperationalError("database or disk is full"),
            OSError("database disk image is malformed"),  # not from SQLite
            None,
        ):
            self.assertFalse(is_database_corruption_error(ordinary), ordinary)


@requires_posix_migration_lock
class PauseMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = HistoryStore(str(self.root / "history.sqlite3"))

    def test_marker_is_durable_first_write_wins_and_holds_only_a_timestamp(self) -> None:
        self.assertEqual(self.store.read_collection_pause(), (False, None))
        first = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(self.store.record_collection_pause(first), first)
        self.assertEqual(
            self.store.record_collection_pause(datetime(2030, 2, 1, tzinfo=timezone.utc)),
            first,
        )
        restarted = HistoryStore(str(self.store.file_path), initialize=False)
        self.assertEqual(restarted.read_collection_pause(), (True, first))
        text = self.store.collection_pause_marker_path.read_text(encoding="ascii")
        self.assertEqual(datetime.fromisoformat(text.strip().replace("Z", "+00:00")), first)
        self.assertNotIn(str(self.root), text)
        self.assertLess(len(text), 64)

    def test_an_unreadable_marker_still_means_paused(self) -> None:
        self.store.collection_pause_marker_path.write_bytes(b"\xff not a time")
        self.assertEqual(self.store.read_collection_pause(), (True, None))
        self.store.collection_pause_marker_path.unlink()
        self.store.collection_pause_marker_path.mkdir()
        self.assertEqual(self.store.read_collection_pause(), (True, None))


def _collector(store: object) -> HistoryCollector:
    return HistoryCollector(HistorySettings(), store)  # type: ignore[arg-type]


@requires_posix_migration_lock
class CollectorPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = HistoryStore(str(self.root / "history.sqlite3"))

    def test_a_corrupt_write_during_a_pass_pauses_every_later_pass(self) -> None:
        collector = _collector(self.store)

        async def damaged_pass(**_: object) -> None:
            raise MALFORMED

        with patch.object(collector, "_run_once_unlocked", damaged_pass):
            with self.assertRaises(sqlite3.DatabaseError):
                asyncio.run(collector.run_once(collection_kind="background"))

        self.assertEqual(collector.collection_pause()[0], True)
        self.assertEqual(collector.degraded_reason(), COLLECTION_PAUSED_REASON)
        status = collector.status()
        self.assertIs(status["history_collection_paused"], True)
        self.assertIsNotNone(status["history_collection_paused_at"])

        untouched = MagicMock()
        with patch.object(collector, "_run_once_unlocked", untouched):
            with self.assertRaises(HistoryCollectionPaused):
                asyncio.run(collector.run_once(force_fast=True))
        untouched.assert_not_called()
        self.assertFalse(collector.collection_running)

        # A restart reads the marker, so the pause survives it.
        restarted = _collector(HistoryStore(str(self.store.file_path)))
        self.assertIs(restarted.status()["history_collection_paused"], True)
        with self.assertRaises(HistoryCollectionPaused):
            asyncio.run(restarted.run_once())

    def test_an_ordinary_failure_does_not_pause(self) -> None:
        collector = _collector(self.store)

        async def locked_pass(**_: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        with patch.object(collector, "_run_once_unlocked", locked_pass):
            with self.assertRaises(sqlite3.OperationalError):
                asyncio.run(collector.run_once())
        self.assertEqual(collector.collection_pause(), (False, None))
        self.assertFalse(self.store.collection_pause_marker_path.exists())

    def test_damage_found_by_retention_pauses_collection(self) -> None:
        store = MagicMock(wraps=self.store)
        store.maintain_retention.side_effect = MALFORMED
        store.read_retention_wait_anchor.return_value = None
        collector = _collector(store)

        collector._run_retention_if_due(datetime(2030, 1, 1, tzinfo=timezone.utc), backup_succeeded=True)

        self.assertIs(collector.collection_pause()[0], True)
        self.assertTrue(self.store.collection_pause_marker_path.exists())

    def test_a_marker_that_cannot_be_written_still_pauses_this_process(self) -> None:
        store = MagicMock(wraps=self.store)
        store.record_collection_pause.side_effect = OSError("read-only file system")
        collector = _collector(store)
        with self.assertLogs("history_service.collector", level="ERROR"):
            collector._pause_for_damage(MALFORMED)
        paused, paused_at = collector.collection_pause()
        self.assertTrue(paused)
        self.assertIsNotNone(paused_at)


@requires_posix_migration_lock
class HealthAndRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = HistoryStore(str(Path(self._temporary.name) / "history.sqlite3"))
        self.collector = _collector(self.store)
        self.store.record_collection_pause(datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_healthz_is_degraded_with_the_pause_reason_and_reads_stay_up(self) -> None:
        with (
            patch.object(history_main, "store", self.store),
            patch.object(history_main, "collector", self.collector),
            patch.object(history_main, "startup_failure_reason", None),
        ):
            response = asyncio.run(history_main.healthz())
            overview = asyncio.run(history_main.overview(exact_counts=False))
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["detail"], COLLECTION_PAUSED_REASON)
        self.assertIs(payload["collector"]["history_collection_paused"], True)
        self.assertIn("counts", overview)
        self.assertNotIn(str(self.store.file_path.parent), response.body.decode())

    def test_manual_refresh_is_refused_while_paused_without_starting_the_cooldown(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/refresh")
        request = MagicMock()
        admission = history_main.ManualRefreshAdmission(cooldown_seconds=900)
        with (
            patch.object(history_main, "collector", self.collector),
            patch.object(history_main, "refresh_admission", admission),
            patch.object(history_main, "authorize_refresh_request"),
            patch.object(history_main, "read_refresh_document", return_value="full"),
        ):
            response = asyncio.run(route.endpoint(request=request))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.body)["detail"], COLLECTION_PAUSED_REASON)
            # After recovery the first full refresh must be admitted at once.
            self.store.clear_collection_pause()
            decision = asyncio.run(admission.try_acquire("full"))
        self.assertTrue(decision.accepted, decision)


@requires_posix_migration_lock
class RecoveryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.path = self.root / "history.sqlite3"
        self.store = HistoryStore(str(self.path))
        # closing(): sqlite3's own context manager commits but never closes,
        # and a late close would checkpoint the WAL in the middle of a test.
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, slot_label, "
                "event_type, details_json) VALUES ('2030-01-01T00:00:00+00:00', 'archive-core', "
                "'enc-a', 1, 'Slot 01', 'disk_inserted', '{}')"
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.store.record_collection_pause(datetime(2030, 1, 1, tzinfo=timezone.utc))

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = recovery.main(list(argv), store=HistoryStore(str(self.path), initialize=False))
        return code, out.getvalue(), err.getvalue()

    def test_status_only_reads(self) -> None:
        before = _digest_tree(self.root)
        code, out, _ = self._run("status")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["database_check"], "ok")
        self.assertIs(report["collection_paused"], True)
        self.assertNotIn(str(self.root), out)
        self.assertEqual(_digest_tree(self.root), before)

    def test_status_and_a_refused_acknowledge_leave_a_rollback_journal_database_untouched(self) -> None:
        # Ordinary reads switch a database to WAL; recovery inspection must not.
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE").fetchall()
        self.store.record_quarantine_recovery(datetime(2030, 1, 1, tzinfo=timezone.utc))
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE").fetchall()
        before = _digest_tree(self.root)

        code, out, _ = self._run("status")

        self.assertEqual(code, 0)
        self.assertIs(json.loads(out)["recovery_required"], True)
        self.assertEqual(_digest_tree(self.root), before)
        # A rollback-journal database gets no sidecars at all from inspection.
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if path.name.endswith(("-wal", "-shm"))), [])
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_acknowledge_refuses_while_the_database_is_damaged_and_changes_nothing(self) -> None:
        # Damage a page past the header of a synthetic copy's own file.
        raw = bytearray(self.path.read_bytes())
        page_size = int.from_bytes(raw[16:18], "big") or 65536
        for offset in range(page_size, min(len(raw), page_size * 3)):
            raw[offset] = 0xA5
        self.path.write_bytes(bytes(raw))
        before = _digest_tree(self.root)

        code, _, err = self._run("acknowledge")

        self.assertEqual(code, recovery.EXIT_REFUSED)
        self.assertIn("Restore a full backup first", err)
        self.assertEqual(_digest_tree(self.root), before)
        self.assertTrue(self.store.collection_pause_marker_path.exists())

    def test_acknowledge_after_recovery_clears_the_pause_and_keeps_rows(self) -> None:
        broken = self.root / "history.sqlite3.broken-20300101T000000.000000Z"
        broken.write_bytes(b"retained quarantine bytes")
        self.store.record_quarantine_recovery(datetime(2030, 1, 1, tzinfo=timezone.utc))

        code, out, _ = self._run("acknowledge")

        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIs(report["cleared_collection_pause"], True)
        self.assertIs(report["acknowledged_quarantine"], True)
        self.assertIs(report["collection_paused"], False)
        self.assertIs(report["recovery_required"], False)
        self.assertFalse(self.store.collection_pause_marker_path.exists())
        self.assertEqual(broken.read_bytes(), b"retained quarantine bytes")
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0], 1)
        collector = _collector(HistoryStore(str(self.path)))
        self.assertEqual(collector.collection_pause(), (False, None))
        self.assertIsNone(collector.degraded_reason())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
