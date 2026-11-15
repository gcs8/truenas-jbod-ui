"""The backup list carries the same upload-check provenance as the details (#1202).

An FTP-style target checks only the remote size at upload, so such a copy is
``verified`` without anyone having read it back. The details already say so
(#722); the list rows must carry the same evidence so the list column can say
the same thing, and must switch to the stronger result only after an explicit
Verify has read the copy back and matched its size and SHA-256.

Synthetic local-directory targets and an in-memory FTP server only; nothing
leaves the temporary directory.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import threading
import unittest

from history_service.backup_archive.catalog import new_artifact_id
from history_service.backup_archive.transport import FtpTarget, LocalDirectoryTarget

import tests.test_backup_archive_transport as archive_transport_fixtures
import tests.test_backup_integration as backup_fixtures


class SizeOnlyTarget(LocalDirectoryTarget):
    """Behaves like the FTP transport: the upload is checked by size only."""

    def put(self, local_path, name):
        return dataclasses.replace(super().put(local_path, name), upload_check="size")


class BackupListAssuranceTests(backup_fixtures.SchedulerTestBase):
    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            if settings.target_id in self.broken_targets:
                raise ConnectionRefusedError("connection refused by nas.example.test")
            yield SizeOnlyTarget(self.remote_root / settings.target_id)

        return opened()

    def _scheduler_with_size_only_copy(self):
        scheduler = self.make({"full": {"enabled": True}, "targets": [backup_fixtures.TARGET]})
        local = scheduler.run_now("full")
        remote = next(r for r in scheduler.catalog.list() if r.location == "nas")
        self.assertTrue(remote.verified)
        return scheduler, local, remote

    def _row(self, scheduler, artifact_id):
        row = next(item for item in scheduler.library()["artifacts"] if item["id"] == artifact_id)
        for key in ("upload_check", "last_verify"):
            self.assertIn(key, row, f"the list row carries no {key} provenance")
        return row

    def test_list_row_says_size_only_before_explicit_verify(self) -> None:
        scheduler, local, remote = self._scheduler_with_size_only_copy()
        row = self._row(scheduler, remote.artifact_id)
        self.assertTrue(row["verified"])
        self.assertEqual(row["upload_check"], "size")
        self.assertIsNone(row["last_verify"])
        detail = scheduler.detail(remote.artifact_id)
        self.assertEqual(row["upload_check"], detail["upload_check"])
        self.assertEqual(row["last_verify"], detail["last_verify"])

        # Control: a local copy has no upload check, in the list and the details.
        local_row = self._row(scheduler, local.artifact_id)
        self.assertIsNone(local_row["upload_check"])
        self.assertIsNone(local_row["last_verify"])
        self.assertIsNone(scheduler.detail(local.artifact_id)["upload_check"])

    def test_list_row_carries_a_successful_explicit_verify_without_its_text(self) -> None:
        scheduler, _local, remote = self._scheduler_with_size_only_copy()
        self.assertTrue(scheduler.verify(remote.artifact_id)["ok"])
        row = self._row(scheduler, remote.artifact_id)
        self.assertEqual(row["upload_check"], "size")
        detail = scheduler.detail(remote.artifact_id)
        self.assertEqual(row["last_verify"], {"at": detail["last_verify"]["at"], "ok": True})
        self.assertIsInstance(row["last_verify"]["at"], str)
        self.assertIs(detail["last_verify"]["ok"], True)
        self.assertIn("detail", detail["last_verify"])  # the full result stays in the details

    def test_list_row_reports_a_failed_explicit_verify(self) -> None:
        scheduler, _local, remote = self._scheduler_with_size_only_copy()
        self.broken_targets = {"nas"}
        result = scheduler.verify(remote.artifact_id)
        self.assertFalse(result["ok"])
        row = self._row(scheduler, remote.artifact_id)
        self.assertTrue(row["verified"])  # unreachable target: state unknown, flag kept
        self.assertEqual(row["upload_check"], "size")
        self.assertIs(row["last_verify"]["ok"], False)
        self.assertNotIn("detail", row["last_verify"])
        self.assertNotIn("nas.example.test", str(row))

    def test_list_row_drops_unrecognised_provenance(self) -> None:
        scheduler, _local, remote = self._scheduler_with_size_only_copy()
        with scheduler._state_lock:
            meta = scheduler._meta[remote.artifact_id]
            meta["upload_check"] = "checksum-of-the-moon"
            meta["last_verify"] = {"at": "2026-09-24T12:00:00Z", "ok": "true", "detail": "x"}
        row = self._row(scheduler, remote.artifact_id)
        self.assertIsNone(row["upload_check"])
        self.assertEqual(row["last_verify"], {"at": "2026-09-24T12:00:00Z", "ok": False})
        with scheduler._state_lock:
            scheduler._meta[remote.artifact_id]["last_verify"] = "yes"
        self.assertIsNone(self._row(scheduler, remote.artifact_id)["last_verify"])
        with scheduler._state_lock:
            scheduler._meta[remote.artifact_id]["last_verify"] = {"at": 1758715200, "ok": True}
        self.assertEqual(self._row(scheduler, remote.artifact_id)["last_verify"], {"at": None, "ok": True})

    def test_one_container_valued_upload_check_does_not_break_the_list(self) -> None:
        from history_service.backup_scheduler.api import build_app

        scheduler, local, remote = self._scheduler_with_size_only_copy()
        for value in ([], {}, ["size"], {"size": True}):
            with self.subTest(upload_check=value):
                with scheduler._state_lock:
                    scheduler._meta[remote.artifact_id]["upload_check"] = value
                    scheduler._save_meta()
                    scheduler._meta = scheduler._load_meta()
                rows = {item["id"]: item for item in scheduler.library()["artifacts"]}
                self.assertEqual(set(rows), {local.artifact_id, remote.artifact_id})
                self.assertIsNone(rows[remote.artifact_id]["upload_check"])  # unknown, not size or readback
                self.assertIsNone(rows[local.artifact_id]["upload_check"])
                self.assertIsNone(scheduler.detail(remote.artifact_id)["upload_check"])
                status, body = backup_fixtures.asgi_call(build_app(scheduler), "GET", "/internal/backups")
                self.assertEqual(status, 200)
                self.assertEqual(len(json.loads(body)["artifacts"]), 2)
        # Control: a well-formed value on the same row still lists as recorded.
        with scheduler._state_lock:
            scheduler._meta[remote.artifact_id]["upload_check"] = "size"
        self.assertEqual(self._row(scheduler, remote.artifact_id)["upload_check"], "size")


class GatedFTP(archive_transport_fixtures.FakeFTP):
    """In-memory FTP server that holds STOR until released and counts RETR."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.retr_calls = 0

    def storbinary(self, cmd, fp, blocksize=8192):
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("synthetic upload gate expired")
        return super().storbinary(cmd, fp, blocksize)

    def retrbinary(self, cmd, callback, blocksize=8192):
        self.retr_calls += 1
        return super().retrbinary(cmd, callback, blocksize)


class RemoteCopyVerifyIsPerCopyTests(backup_fixtures.SchedulerTestBase):
    """A Verify of one copy never makes another copy look read back."""

    def setUp(self) -> None:
        super().setUp()
        self.ftp = GatedFTP()
        self.ftp.alter_same_length = True  # the stored bytes differ, the size matches
        self.addCleanup(self.ftp.release.set)

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            yield FtpTarget(self.ftp, "/backups", encrypted=False)

        return opened()

    def test_local_verify_during_upload_does_not_verify_the_remote_copy(self) -> None:
        scheduler = self.make({"full": {"enabled": True}, "targets": [backup_fixtures.TARGET]})
        worker = scheduler.start_run("full")
        self.addCleanup(worker.join, 10)
        self.assertTrue(self.ftp.entered.wait(10), "the upload was not reached")
        local = next(r for r in scheduler.catalog.list() if r.location == "local")
        self.assertTrue(scheduler.verify(local.artifact_id)["ok"])
        self.ftp.release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())

        remote = next(r for r in scheduler.catalog.list() if r.location == "nas")
        self.assertEqual(self.ftp.retr_calls, 0)  # nobody read the remote copy back
        row = self._row(scheduler, remote.artifact_id)
        self.assertEqual(row["upload_check"], "size")
        self.assertIsNone(row["last_verify"], "the remote copy inherited the local copy's Verify")
        detail = scheduler.detail(remote.artifact_id)
        self.assertIsNone(detail["last_verify"])
        # The archive description still travels with the copy.
        self.assertEqual(detail["inspect"], scheduler.detail(local.artifact_id)["inspect"])
        self.assertIsNotNone(detail["inspect"])
        # Control: the local copy keeps its own successful Verify.
        self.assertEqual(self._row(scheduler, local.artifact_id)["last_verify"]["ok"], True)

        # The remote copy's own Verify reads it back and finds the altered bytes.
        result = scheduler.verify(remote.artifact_id)
        self.assertFalse(result["ok"])
        self.assertEqual(self.ftp.retr_calls, 1)
        self.assertIs(self._row(scheduler, remote.artifact_id)["last_verify"]["ok"], False)

    def test_remote_metadata_carries_only_the_archive_description(self) -> None:
        scheduler = self.make({"full": {"enabled": True}, "targets": [backup_fixtures.TARGET]})
        self.ftp.release.set()
        self.ftp.alter_same_length = False
        local = scheduler.run_now("full")
        remote = next(r for r in scheduler.catalog.list() if r.location == "nas")
        local_meta = scheduler._metadata_snapshot(local.artifact_id)
        # Per-copy state planted on the source must never reach a new copy.
        with scheduler._state_lock:
            scheduler._meta[local.artifact_id]["last_verify"] = {"at": "2026-09-24T12:00:00Z", "ok": True, "detail": "x"}
            scheduler._meta[local.artifact_id]["publication"] = {"name": "planted"}
            scheduler._meta[local.artifact_id]["future_per_copy_fact"] = 1
        copy = dataclasses.replace(remote, artifact_id=new_artifact_id(), location="second")
        scheduler._catalog_remote(local, copy, upload_check="readback")
        copy_meta = scheduler._metadata_snapshot(copy.artifact_id)
        self.assertNotIn("last_verify", copy_meta)
        self.assertNotIn("publication", copy_meta)
        self.assertNotIn("future_per_copy_fact", copy_meta)
        self.assertEqual(copy_meta["upload_check"], "readback")
        for key in ("app_version", "schema_version", "packaging", "groups"):
            self.assertEqual(copy_meta.get(key), local_meta.get(key), key)

    def _row(self, scheduler, artifact_id):
        return next(item for item in scheduler.library()["artifacts"] if item["id"] == artifact_id)


if __name__ == "__main__":
    unittest.main()
