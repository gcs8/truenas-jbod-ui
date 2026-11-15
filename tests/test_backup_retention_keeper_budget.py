"""Retention keeper checks stay bounded and fail closed (#1188).

Before a retention deletion, grooming reads back the newest kept copy and stops
as soon as one kept copy matches. A missing or mismatching keeper is downgraded
and the next one is tried, but only a small fixed number of reads per class and
location; running out of reads refuses the deletions. Synthetic files only: the
remote target is the real filesystem transport over a temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from history_service.backup_archive.catalog import ArtifactRecord
from history_service.backup_archive.lifecycle import (
    ApplyResult,
    LifecycleManager,
    RetentionRule,
    local_payload_digest,
    local_target_resolver,
)
from history_service.backup_archive.transport import ArchiveTransportError, LocalDirectoryTarget
from history_service.backup_scheduler import service as scheduler_service
from tests.test_backup_archive_lifecycle import NOW, CatalogCase
from tests.test_backup_integration import TARGET, SchedulerTestBase
from tests.wait_bounds import HANG_TIMEOUT_SECONDS

BUDGET_REASON = "keeper-check-budget-exhausted"


class KeeperReadScopeTests(CatalogCase):
    """One passing keeper is enough; the reads per class and location are capped."""

    def setUp(self) -> None:
        super().setUp()
        self.backup_dir = Path(self._tmp.name) / "backups"
        (self.backup_dir / "full").mkdir(parents=True)
        self.reads: list[str] = []

    def add_copy(self, age_days: int, *, corrupt: bool = False) -> tuple[ArtifactRecord, Path]:
        content = f"synthetic full archive {age_days:04d}".encode()
        name = f"full/copy-{age_days:04d}.tar.zst"
        path = self.backup_dir / name
        path.write_bytes(b"X" * len(content) if corrupt else content)
        record = ArtifactRecord(
            artifact_id=f"full-{age_days:04d}",
            backup_class="full",
            location="local",
            name=name,
            created_at=NOW - timedelta(days=age_days),
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            verified=True,
        )
        return self.catalog.add(record, now=NOW), path

    def counting_digest(self, record: ArtifactRecord, target) -> tuple[int, str]:
        self.reads.append(record.artifact_id)
        return local_payload_digest(record, target)

    def groom(self, keep_count: int) -> ApplyResult:
        manager = LifecycleManager(
            self.catalog,
            (RetentionRule("full", "local", keep_count=keep_count),),
            payload_digest=self.counting_digest,
        )
        return manager.apply(manager.plan(NOW), local_target_resolver(self.backup_dir), now=lambda: NOW)

    def test_a_passing_newest_keeper_ends_the_check(self) -> None:
        newest, _ = self.add_copy(1)
        self.add_copy(2, corrupt=True)  # a later keeper that is never read
        self.add_copy(3)
        older = [self.add_copy(day) for day in (4, 5)]

        result = self.groom(keep_count=3)

        self.assertEqual(self.reads, [newest.artifact_id], "only the newest verified keeper is read")
        self.assertTrue(result.complete)
        self.assertEqual(
            sorted(item.record.artifact_id for item in result.deleted),
            sorted(record.artifact_id for record, _path in older),
        )
        self.assertEqual(result.downgraded, ())

    def test_reading_stops_once_no_deletion_remains(self) -> None:
        newest, _ = self.add_copy(1, corrupt=True)
        middle, _ = self.add_copy(2, corrupt=True)
        oldest, oldest_path = self.add_copy(3)

        result = self.groom(keep_count=1)

        # Two downgrades leave nothing to delete, so the oldest copy is not read.
        self.assertEqual(self.reads, [newest.artifact_id, middle.artifact_id])
        self.assertEqual(result.deleted, ())
        self.assertEqual(result.failed_locations, {})
        self.assertTrue(oldest_path.exists())
        self.assertTrue(self.catalog.get(oldest.artifact_id).verified)

    def test_cascading_corrupt_keepers_stop_at_the_read_budget(self) -> None:
        corrupt = [self.add_copy(day, corrupt=True)[0] for day in range(1, 20)]
        oldest, oldest_path = self.add_copy(20)

        result = self.groom(keep_count=1)

        self.assertEqual(self.reads, [record.artifact_id for record in corrupt[:3]])
        self.assertEqual(result.deleted, ())
        self.assertTrue(oldest_path.exists())
        self.assertIn(BUDGET_REASON, result.failed_locations["local"])
        self.assertEqual([record.artifact_id for record in result.downgraded], self.reads)
        for record in corrupt[3:]:
            self.assertTrue(self.catalog.get(record.artifact_id).verified, "unread copies keep their flag")

    def test_running_out_of_reads_never_permits_a_deletion(self) -> None:
        for day in (1, 2, 3):
            self.add_copy(day, corrupt=True)
        good, good_path = self.add_copy(4)  # would pass, but lies past the budget
        older = [self.add_copy(day) for day in (5, 6)]

        result = self.groom(keep_count=1)

        self.assertEqual(len(self.reads), 3)
        self.assertNotIn(good.artifact_id, self.reads)
        self.assertEqual(result.deleted, ())
        self.assertIn(BUDGET_REASON, result.failed_locations["local"])
        self.assertTrue(good_path.exists())
        for record, path in older:
            self.assertTrue(path.exists(), "budget exhaustion deleted a copy")
            self.assertIsNotNone(self.catalog.get(record.artifact_id))


class SchedulerKeeperCheckTests(SchedulerTestBase):
    """Keeper checks through the scheduler and the real filesystem transport."""

    def two_remote_fulls(self) -> tuple[ArtifactRecord, ArtifactRecord]:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 2, "remote_keep": 2}, "targets": [TARGET]})
        scheduler.run_now("full")
        self.now += timedelta(hours=1)
        scheduler.run_now("full")
        older, newest = scheduler.catalog.list(backup_class="full", location="nas")
        scheduler.close()
        return older, newest

    def remote_keep_one(self):
        return self.make({"full": {"enabled": True, "local_keep": 2, "remote_keep": 1}, "targets": [TARGET]})

    def scratch_dirs(self) -> list[str]:
        return sorted(p.name for p in self._paths.state_dir.iterdir() if p.name.startswith("keeper-check-"))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs POSIX FIFOs")
    def test_remote_filesystem_fifo_keeper_is_refused_without_blocking(self) -> None:
        older, newest = self.two_remote_fulls()
        remote_dir = self.remote_root / "nas"
        fifo = remote_dir / newest.name
        fifo.unlink()
        os.mkfifo(fifo)
        scheduler = self.remote_keep_one()
        token = scheduler.plan()[0]
        outcome: list = []
        worker = threading.Thread(target=lambda: outcome.append(scheduler.apply(token)), daemon=True)
        worker.start()
        worker.join(HANG_TIMEOUT_SECONDS)
        if worker.is_alive():
            # Give the blocked reader a writer so the thread can finish, then fail.
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(HANG_TIMEOUT_SECONDS)
            self.fail("a FIFO in place of a remote filesystem keeper blocked grooming")

        [result] = outcome
        self.assertEqual(result.deleted, ())
        self.assertIn("keeper-readback-failed", result.failed_locations["nas"])
        self.assertTrue((remote_dir / older.name).exists())
        self.assertTrue(scheduler.catalog.get(newest.artifact_id).verified, "an unchecked keeper is not downgraded")
        self.assertIsNone(scheduler.running)
        self.assertEqual(self.scratch_dirs(), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs POSIX FIFOs")
    def test_filesystem_target_get_refuses_a_fifo_without_blocking(self) -> None:
        root = self.root / "fifo-target"
        (root / "full").mkdir(parents=True)
        os.mkfifo(root / "full" / "copy.tar.zst")
        target = LocalDirectoryTarget(root)
        errors: list[BaseException] = []

        def read() -> None:
            try:
                target.get("full/copy.tar.zst", self.root / "readback", limit=16)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(HANG_TIMEOUT_SECONDS)
        if worker.is_alive():
            os.close(os.open(root / "full" / "copy.tar.zst", os.O_WRONLY | os.O_NONBLOCK))
            worker.join(HANG_TIMEOUT_SECONDS)
            self.fail("opening a FIFO as a transport source blocked")
        [error] = errors
        self.assertIsInstance(error, ArchiveTransportError)
        self.assertFalse((self.root / "readback").exists())

    def test_sidecar_credit_check_caps_attempted_reads(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 100}})
        records = []
        for _ in range(21):
            records.append(scheduler.run_now("full"))
            self.now += timedelta(days=1)
        # The 20 newest are corrupted without a size change; the oldest is intact.
        for record in records[1:]:
            (self._paths.local_dir / record.name).write_bytes(b"X" * record.size)
        reads: list[str] = []

        def counting(record, target):
            reads.append(record.artifact_id)
            return local_payload_digest(record, target)

        with patch.object(scheduler_service, "local_payload_digest", side_effect=counting):
            credit = scheduler._verified_local_full_count()

        self.assertEqual(len(reads), scheduler_service.HISTORY_REPLACEMENT_COPIES)
        self.assertEqual(credit, 0, "credit counts only copies inside the attempt cap")
        self.assertTrue(scheduler.catalog.get(records[0].artifact_id).verified, "the unread copy keeps its flag")

    def test_credit_check_downgrade_unpublishes_the_full_receipt(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 100}})
        real_ship = scheduler._ship

        def ship_then_corrupt(record):
            failures = real_ship(record)
            # The just-published FULL is damaged in place without a size change
            # before the run's sidecar credit check reads it.
            (self._paths.local_dir / record.name).write_bytes(b"X" * record.size)
            return failures

        with patch.object(scheduler, "_ship", side_effect=ship_then_corrupt):
            record = scheduler.run_now("full")

        self.assertFalse(scheduler.catalog.get(record.artifact_id).verified)
        status = json.loads(self._paths.status_file.read_text())
        self.assertNotIn("verified_full", status, "a receipt for a known-bad FULL stayed published")

    def test_leftover_keeper_scratch_is_removed_before_a_remote_check(self) -> None:
        older, _newest = self.two_remote_fulls()
        leftover = self._paths.state_dir / "keeper-check-interrupted"
        leftover.mkdir(mode=0o700)
        (leftover / "archive").write_bytes(b"partial readback from a killed run")
        scheduler = self.remote_keep_one()

        result = scheduler.apply(scheduler.plan()[0])

        self.assertEqual([item.record.artifact_id for item in result.deleted], [older.artifact_id])
        self.assertEqual(self.scratch_dirs(), [])

    def test_remote_check_refuses_when_scratch_space_is_short(self) -> None:
        older, newest = self.two_remote_fulls()
        scheduler = self.remote_keep_one()

        for attempt in range(2):
            with self.subTest(attempt=attempt), patch.object(
                scheduler_service, "_scratch_free_bytes", return_value=newest.size
            ):
                result = scheduler.apply(scheduler.plan()[0])
                self.assertEqual(result.deleted, ())
                self.assertIn("keeper-scratch-capacity", result.failed_locations["nas"])
                self.assertTrue((self.remote_root / "nas" / older.name).exists())
                self.assertTrue(scheduler.catalog.get(newest.artifact_id).verified)
                self.assertEqual(self.scratch_dirs(), [])
        grooming = json.loads(self._paths.status_file.read_text())["grooming"]
        self.assertIn("keeper-scratch-capacity", grooming["failed_locations"]["nas"])


if __name__ == "__main__":
    unittest.main()
