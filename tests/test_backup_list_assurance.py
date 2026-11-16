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
        own = remote.artifact_id  # results bound to this copy, so the sanitiser is reached
        with scheduler._state_lock:
            meta = scheduler._meta[remote.artifact_id]
            meta["upload_check"] = "checksum-of-the-moon"
            meta["last_verify"] = {"at": "2026-09-24T12:00:00Z", "ok": "true", "detail": "x", "artifact_id": own}
        row = self._row(scheduler, remote.artifact_id)
        self.assertIsNone(row["upload_check"])
        self.assertEqual(row["last_verify"], {"at": "2026-09-24T12:00:00Z", "ok": False})
        with scheduler._state_lock:
            scheduler._meta[remote.artifact_id]["last_verify"] = "yes"
        self.assertIsNone(self._row(scheduler, remote.artifact_id)["last_verify"])
        with scheduler._state_lock:
            scheduler._meta[remote.artifact_id]["last_verify"] = {"at": 1758715200, "ok": True, "artifact_id": own}
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
            scheduler._meta[local.artifact_id]["history_replacement"] = True
        local_meta["history_replacement"] = True
        copy = dataclasses.replace(remote, artifact_id=new_artifact_id(), location="second")
        scheduler._catalog_remote(local, copy, upload_check="readback")
        copy_meta = scheduler._metadata_snapshot(copy.artifact_id)
        self.assertNotIn("last_verify", copy_meta)
        self.assertNotIn("publication", copy_meta)
        self.assertNotIn("future_per_copy_fact", copy_meta)
        self.assertEqual(copy_meta["upload_check"], "readback")
        for key in ("app_version", "schema_version", "packaging", "groups", "history_replacement"):
            self.assertIn(key, copy_meta, key)
            self.assertEqual(copy_meta[key], local_meta.get(key), key)

    def _row(self, scheduler, artifact_id):
        return next(item for item in scheduler.library()["artifacts"] if item["id"] == artifact_id)


LEGACY_RESULT = {"at": "2026-09-24T11:00:00+00:00", "ok": True, "detail": "Readback matched the catalogued size and SHA-256."}


class VerifyResultIsBoundToItsCopyTests(backup_fixtures.SchedulerTestBase):
    """A Verify result counts only for the copy whose own bytes it read.

    Earlier releases copied the local copy's whole metadata, Verify result
    included, into each remote copy. Such state survives an upgrade: the
    remote copy must read as unknown (its upload check only) until its own
    Verify reads it back. Listing never reads a remote copy and never rewrites
    the metadata file.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ftp = GatedFTP()
        self.ftp.release.set()

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            yield FtpTarget(self.ftp, "/backups", encrypted=False)

        return opened()

    def _config(self):
        return {"full": {"enabled": True}, "targets": [backup_fixtures.TARGET]}

    def _legacy_state(self):
        """Catalogue and metadata files as the earlier release left them."""

        scheduler = self.make(self._config())
        local = scheduler.run_now("full")
        remote = next(r for r in scheduler.catalog.list() if r.location == "nas")
        with scheduler._state_lock:
            # The earlier Verify wrote no copy binding, and the earlier upload
            # copied the local copy's result into the remote copy's metadata.
            scheduler._meta[local.artifact_id]["last_verify"] = dict(LEGACY_RESULT)
            scheduler._meta[remote.artifact_id]["last_verify"] = dict(LEGACY_RESULT)
            scheduler._save_meta()
        scheduler.close()
        return local, remote

    def _row(self, scheduler, artifact_id):
        return next(item for item in scheduler.library()["artifacts"] if item["id"] == artifact_id)

    def _http(self, app, method, path, body=None):
        status, payload = backup_fixtures.asgi_call(app, method, path, body)
        self.assertEqual(status, 200, payload)
        return json.loads(payload)

    def test_an_inherited_result_from_an_earlier_release_is_unknown_after_restart(self) -> None:
        from history_service.backup_scheduler.api import build_app

        self.ftp.alter_same_length = True  # the stored bytes differ, the size matches
        local, remote = self._legacy_state()
        meta_path = self._paths.state_dir / "artifact-meta.json"
        before = meta_path.read_bytes()

        scheduler = self.make(self._config())  # restart on the new code
        app = build_app(scheduler)
        row = self._row(scheduler, remote.artifact_id)
        self.assertEqual(row["upload_check"], "size")
        self.assertIsNone(row["last_verify"], "an inherited Verify result counted for the remote copy")
        self.assertIsNone(scheduler.detail(remote.artifact_id)["last_verify"])
        listed = {item["id"]: item for item in self._http(app, "GET", "/internal/backups")["artifacts"]}
        self.assertIsNone(listed[remote.artifact_id]["last_verify"])
        self.assertIsNone(self._http(app, "GET", f"/internal/backups/{remote.artifact_id}")["last_verify"])
        kept = self._http(app, "POST", f"/internal/backups/{remote.artifact_id}/preserve", {"reason": "synthetic keep"})
        self.assertIsNone(kept["artifact"]["last_verify"])
        released = self._http(app, "DELETE", f"/internal/backups/{remote.artifact_id}/preserve")
        self.assertIsNone(released["artifact"]["last_verify"])
        # Control: the local copy's own result, written by its own Verify, still counts.
        self.assertEqual(self._row(scheduler, local.artifact_id)["last_verify"], {"at": LEGACY_RESULT["at"], "ok": True})
        self.assertIs(scheduler.detail(local.artifact_id)["last_verify"]["ok"], True)
        # Nothing read the remote copy, and nothing rewrote the metadata file.
        self.assertEqual(self.ftp.retr_calls, 0)
        self.assertEqual(meta_path.read_bytes(), before)

        # The remote copy's own Verify reads it back and finds the altered bytes.
        result = scheduler.verify(remote.artifact_id)
        self.assertFalse(result["ok"])
        self.assertEqual(self.ftp.retr_calls, 1)
        self.assertEqual(result["artifact"]["last_verify"]["ok"], False)
        self.assertIs(self._row(scheduler, remote.artifact_id)["last_verify"]["ok"], False)

    def test_the_copys_own_verify_after_the_upgrade_counts(self) -> None:
        _local, remote = self._legacy_state()
        scheduler = self.make(self._config())
        self.assertIsNone(self._row(scheduler, remote.artifact_id)["last_verify"])
        result = scheduler.verify(remote.artifact_id)
        self.assertTrue(result["ok"])
        self.assertEqual(self.ftp.retr_calls, 1)
        row = self._row(scheduler, remote.artifact_id)
        self.assertIs(row["last_verify"]["ok"], True)
        self.assertEqual(row["last_verify"], result["artifact"]["last_verify"])
        detail = scheduler.detail(remote.artifact_id)["last_verify"]
        self.assertEqual(set(detail), {"at", "ok", "detail"})
        self.assertIs(detail["ok"], True)
        # The stored result names the copy it read.
        self.assertEqual(scheduler._metadata_snapshot(remote.artifact_id)["last_verify"]["artifact_id"], remote.artifact_id)

    def test_a_result_bound_to_another_copy_never_counts(self) -> None:
        scheduler = self.make(self._config())
        local = scheduler.run_now("full")
        remote = next(r for r in scheduler.catalog.list() if r.location == "nas")
        self.assertTrue(scheduler.verify(local.artifact_id)["ok"])
        own = scheduler._metadata_snapshot(local.artifact_id)["last_verify"]
        self.assertEqual(own.get("artifact_id"), local.artifact_id)
        # Whatever path copies it, the local copy's result is not the remote copy's.
        with scheduler._state_lock:
            scheduler._meta[remote.artifact_id]["last_verify"] = dict(own)
        self.assertIsNone(self._row(scheduler, remote.artifact_id)["last_verify"])
        self.assertIsNone(scheduler.detail(remote.artifact_id)["last_verify"])
        self.assertIsNone(scheduler.serialize(remote)["last_verify"])
        self.assertEqual(self.ftp.retr_calls, 0)
        # Control: the local copy keeps it.
        self.assertIs(self._row(scheduler, local.artifact_id)["last_verify"]["ok"], True)

    def test_details_count_only_a_boolean_true_as_passed(self) -> None:
        from history_service.backup_scheduler.api import build_app

        scheduler = self.make(self._config())
        scheduler.run_now("full")
        remote = next(r for r in scheduler.catalog.list() if r.location == "nas")
        app = build_app(scheduler)
        cases = (
            ({"at": "2026-09-24T12:00:00Z", "ok": "true", "detail": "x"}, {"at": "2026-09-24T12:00:00Z", "ok": False, "detail": "x"}),
            ({"at": 1758715200, "ok": 1, "detail": ["x"]}, {"at": None, "ok": False, "detail": None}),
            ({"at": "2026-09-24T12:00:00Z", "ok": True, "detail": "y"}, {"at": "2026-09-24T12:00:00Z", "ok": True, "detail": "y"}),
            ({"at": "2026-09-24T12:00:00Z", "ok": False, "detail": "z"}, {"at": "2026-09-24T12:00:00Z", "ok": False, "detail": "z"}),
        )
        for stored, expected in cases:
            with self.subTest(stored=stored):
                with scheduler._state_lock:
                    scheduler._meta[remote.artifact_id]["last_verify"] = {**stored, "artifact_id": remote.artifact_id}
                self.assertEqual(scheduler.detail(remote.artifact_id)["last_verify"], expected)
                self.assertEqual(self._http(app, "GET", f"/internal/backups/{remote.artifact_id}")["last_verify"], expected)
                self.assertEqual(
                    self._row(scheduler, remote.artifact_id)["last_verify"], {"at": expected["at"], "ok": expected["ok"]}
                )


if __name__ == "__main__":
    unittest.main()
