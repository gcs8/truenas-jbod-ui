"""History retention, backups and footprint on a v1 (unsegmented) install (#455).

Retention used to accept only the history sidecar's own snapshot copies as a
usable backup. Since #580 the backup scheduler's full class also saves the
history database; a recent successful one now counts too, so a failing sidecar
backup directory no longer delays pruning while real full backups work. The
dashboard also shows the sidecar copies' footprint and the free space inside
the database file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from history_service.collector import HistoryCollector
from history_service.config import HistorySettings
from history_service import main as history_main
from history_service.main import backup_footprint_label, build_dashboard_context, reclaimable_label
from history_service.store import HistoryStore

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _status(*, success_at: datetime, groups: list[str] | None = None, error_code: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "enabled": True,
        "included_groups": groups or ["config_file", "history_db"],
        "success_count": 1,
        "failure_count": 1 if error_code else 0,
        "last_attempt_at": success_at.isoformat(),
        "last_success_at": success_at.isoformat(),
        "last_failure_at": success_at.isoformat() if error_code else None,
        "last_size_bytes": 123,
        "last_sha256": "a" * 64,
        "last_artifact_name": "jbod-scheduled-backup-20300102T030405Z-00000001.7z",
        "last_absent_groups": [],
        "last_retention_removed": 0,
        "last_error_code": error_code,
    }


def _store() -> MagicMock:
    store = MagicMock()
    anchor: dict[str, datetime] = {}
    store.read_retention_wait_anchor.side_effect = lambda: anchor.get("at")
    store.start_retention_wait.side_effect = lambda at: anchor.setdefault("at", at)
    store.latest_backup_snapshot_at.return_value = None
    store.maintain_retention.return_value = {"total_rows_removed": 0, "has_more": False}
    return store


class RetentionAcceptsScheduledFullBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.status_path = Path(self.temp.name) / "scheduled-backup.json"

    def _collector(self, store: MagicMock, *, status_file: bool = True) -> HistoryCollector:
        return HistoryCollector(
            HistorySettings(
                sqlite_path=str(Path(self.temp.name) / "history.sqlite3"),
                backup_dir=str(Path(self.temp.name) / "backups"),
                scheduled_backup_status_file=str(self.status_path) if status_file else None,
                raw_metric_retention_days=30,
                retention_backup_skip_max_seconds=86400,
            ),
            store,
        )

    def _write_status(self, payload: dict, mode: int = 0o640) -> None:
        self.status_path.write_text(json.dumps(payload), encoding="utf-8")
        self.status_path.chmod(mode)

    def test_a_recent_full_backup_lets_retention_run_while_sidecar_backups_fail(self) -> None:
        store = _store()
        self._write_status(_status(success_at=NOW - timedelta(hours=6)))
        collector = self._collector(store)

        collector._run_retention_if_due(NOW, backup_succeeded=False)

        store.maintain_retention.assert_called_once()
        status = collector.status()
        self.assertIsNone(status["last_retention_skip_reason"])
        self.assertFalse(status["last_retention_ran_without_backup"])

    def test_without_the_full_backup_the_bounded_wait_is_unchanged(self) -> None:
        store = _store()
        collector = self._collector(store, status_file=False)

        collector._run_retention_if_due(NOW, backup_succeeded=False)

        store.maintain_retention.assert_not_called()
        self.assertEqual(
            collector.status()["last_retention_skip_reason"],
            "Waiting for a successful database backup before pruning.",
        )

    def test_a_full_backup_that_does_not_hold_history_does_not_count(self) -> None:
        cases = {
            "config only": _status(success_at=NOW - timedelta(hours=1), groups=["config_file"]),
            "last run failed": _status(success_at=NOW - timedelta(hours=1), error_code="write_failed"),
            "older than raw retention": _status(success_at=NOW - timedelta(days=31)),
            "from the future": _status(success_at=NOW + timedelta(hours=1)),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                store = _store()
                self._write_status(payload)
                collector = self._collector(store)

                collector._run_retention_if_due(NOW, backup_succeeded=False)

                store.maintain_retention.assert_not_called()

    def test_a_group_writable_status_file_is_not_trusted(self) -> None:
        store = _store()
        self._write_status(_status(success_at=NOW - timedelta(hours=1)), mode=0o660)
        collector = self._collector(store)

        collector._run_retention_if_due(NOW, backup_succeeded=False)

        store.maintain_retention.assert_not_called()


class FootprintAndReclaimableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = HistoryStore(str(self.root / "history.sqlite3"))

    def test_footprint_counts_daily_weekly_and_monthly_copies_read_only(self) -> None:
        daily = self.root / "backups"
        long_term = daily / "long-term"
        for path, size in (
            (daily / "history-20300101T000000Z.sqlite3", 100),
            (daily / "history-20300102T000000Z.sqlite3", 200),
            (long_term / "weekly" / "history-weekly-2030-W01.sqlite3", 300),
            (long_term / "monthly" / "history-monthly-2030-01.sqlite3", 400),
            (daily / "unrelated.txt", 999),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
        before = sorted((path, path.stat().st_mtime_ns) for path in daily.rglob("*"))

        footprint = self.store.backup_footprint(daily, long_term)

        self.assertEqual(footprint, {"copies": 4, "bytes": 1000})
        self.assertEqual(sorted((path, path.stat().st_mtime_ns) for path in daily.rglob("*")), before)
        self.assertEqual(backup_footprint_label(footprint), "1000 B in 4 copies")
        self.assertEqual(self.store.backup_footprint(self.root / "missing"), {"copies": 0, "bytes": 0})
        self.assertEqual(backup_footprint_label({"copies": 0, "bytes": 0}), "no copies")

    def test_reclaimable_reports_free_pages_in_bytes(self) -> None:
        self.assertEqual(self.store.reclaimable_bytes(), 0)
        with closing(sqlite3.connect(self.store.file_path)) as connection:
            connection.execute("CREATE TABLE filler (payload BLOB)")
            connection.executemany("INSERT INTO filler VALUES (?)", ((b"x" * 4000,) for _ in range(200)))
            connection.commit()
            connection.execute("DELETE FROM filler")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]

        self.assertGreater(free_pages, 0)
        self.assertEqual(self.store.reclaimable_bytes(), free_pages * page_size)

    def test_reclaimable_share_divides_by_the_main_file_not_wal_and_shm(self) -> None:
        # #597: free pages live in the main file, so a large WAL must not
        # shrink the displayed share.
        with closing(sqlite3.connect(self.store.file_path)) as reader, closing(
            sqlite3.connect(self.store.file_path)
        ) as connection:
            connection.execute("CREATE TABLE keeper (payload BLOB)")
            connection.executemany("INSERT INTO keeper VALUES (?)", ((b"k" * 4000,) for _ in range(50)))
            connection.execute("CREATE TABLE filler (payload BLOB)")
            connection.executemany("INSERT INTO filler VALUES (?)", ((b"x" * 4000,) for _ in range(200)))
            connection.commit()
            connection.execute("DELETE FROM filler")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
            # The open read transaction pins the WAL so it stays on disk.
            # Rewriting existing rows grows the WAL without using free pages.
            for round_number in range(12):
                connection.execute("UPDATE keeper SET payload = ?", (bytes([round_number]) * 4000,))
                connection.commit()
            main_file = self.store.main_file_size_bytes()
            main_file_on_disk = self.store.file_path.stat().st_size
            total = self.store.database_size_bytes()
            reclaimable = self.store.reclaimable_bytes()
            reader.rollback()

        self.assertEqual(main_file, main_file_on_disk)
        self.assertGreater(total, main_file * 1.5)
        self.assertIsNotNone(reclaimable)
        expected_share = min(100, round(100 * reclaimable / main_file))
        self.assertNotEqual(expected_share, min(100, round(100 * reclaimable / total)))
        self.assertTrue(reclaimable_label(reclaimable, main_file).endswith(f"({expected_share}%)"))

        context = build_dashboard_context(
            request=MagicMock(),
            status={},
            counts={},
            scopes=[],
            app_version="test",
            database_size_bytes=total,
            reclaimable_bytes=reclaimable,
            main_file_size_bytes=main_file,
        )
        self.assertTrue(context["reclaimable_label"].endswith(f"({expected_share}%)"))

    def test_overview_payload_carries_the_disk_metrics_for_polling(self) -> None:
        # #597: the dashboard polls /api/history/overview, so the free-space
        # and backup-footprint labels must ride in that payload.
        with (
            patch.object(history_main, "store", self.store),
            patch.object(history_main.collector, "status", return_value={"collector_running": True}),
            patch.object(self.store, "reclaimable_bytes", return_value=2048),
            patch.object(self.store, "main_file_size_bytes", return_value=8192),
            patch.object(self.store, "database_size_bytes", return_value=40960),
            patch.object(self.store, "backup_footprint", return_value={"copies": 2, "bytes": 3072}),
        ):
            payload = asyncio.run(history_main.overview(exact_counts=False))

        database = payload["database"]
        self.assertEqual(database["size_bytes"], 40960)
        self.assertEqual(database["reclaimable_bytes"], 2048)
        self.assertEqual(database["main_file_size_bytes"], 8192)
        self.assertEqual(database["reclaimable_label"], "2.0 KiB (25%)")
        self.assertEqual(database["backup_footprint"], {"copies": 2, "bytes": 3072})
        self.assertEqual(database["backup_footprint_label"], "3.0 KiB in 2 copies")
        json.dumps(payload)

    def test_reclaimable_label(self) -> None:
        self.assertEqual(reclaimable_label(None, 1000), "unknown")
        self.assertEqual(reclaimable_label(0, 1000), "0 B")
        self.assertEqual(reclaimable_label(2048, 8192), "2.0 KiB (25%)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
