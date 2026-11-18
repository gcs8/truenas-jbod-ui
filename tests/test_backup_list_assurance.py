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
import os
import threading
import unittest

from history_service.backup_archive.catalog import CatalogError, new_artifact_id
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
        self.assertEqual(set(detail), {"at", "ok", "outcome", "detail"})
        self.assertIs(detail["ok"], True)
        self.assertEqual(detail["outcome"], "matched")
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
        # Results from earlier releases carry no outcome.
        cases = tuple((stored, {**expected, "outcome": None}) for stored, expected in cases)
        for stored, expected in cases:
            with self.subTest(stored=stored):
                with scheduler._state_lock:
                    scheduler._meta[remote.artifact_id]["last_verify"] = {**stored, "artifact_id": remote.artifact_id}
                self.assertEqual(scheduler.detail(remote.artifact_id)["last_verify"], expected)
                self.assertEqual(self._http(app, "GET", f"/internal/backups/{remote.artifact_id}")["last_verify"], expected)
                self.assertEqual(
                    self._row(scheduler, remote.artifact_id)["last_verify"], {"at": expected["at"], "ok": expected["ok"]}
                )


S3_TARGET = {**backup_fixtures.TARGET, "target_id": "cloud", "label": "Cloud bucket", "provider": "s3",
             "root": "archive", "bucket": "synthetic"}
EITHER = "provider_checksum_or_readback"


class ProviderChecksumIsNotAReadbackTests(backup_fixtures.SchedulerTestBase):
    """A checksum the storage provider reports is not this application reading the copy back.

    The real S3 target over the maintained in-memory client: a matching ETag
    is checked without a download, an opaque one is downloaded and hashed.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client = archive_transport_fixtures.FakeS3Client()

    def open_target(self, settings):
        from types import SimpleNamespace

        from history_service.backup_archive import transport

        @contextlib.contextmanager
        def opened():
            if settings.provider != "s3":
                yield LocalDirectoryTarget(self.remote_root / settings.target_id)
                return
            yield transport.S3Target(
                self.client, "synthetic", "archive",
                transfer_config=SimpleNamespace(
                    multipart_threshold=transport.S3_MULTIPART_THRESHOLD,
                    multipart_chunksize=transport.S3_MULTIPART_CHUNKSIZE,
                ),
                encrypted=True, connection_errors=(),
            )

        return opened()

    def _config(self, *, with_s3: bool = True):
        access, secret = self.root / "synthetic-access", self.root / "synthetic-secret"
        for path in (access, secret):
            path.write_text("synthetic-only")
            path.chmod(0o600)
        target = {**S3_TARGET, "access_key_id_file": str(access), "secret_access_key_file": str(secret)}
        targets = [target, backup_fixtures.TARGET] if with_s3 else [backup_fixtures.TARGET]
        return {"full": {"enabled": True}, "targets": targets}

    def _views(self, scheduler, artifact_id):
        """The copy as the scheduler, its HTTP list and its HTTP details present it."""

        from history_service.backup_scheduler.api import build_app

        app = build_app(scheduler)
        status, body = backup_fixtures.asgi_call(app, "GET", "/internal/backups")
        self.assertEqual(status, 200)
        listed = next(item for item in json.loads(body)["artifacts"] if item["id"] == artifact_id)
        status, body = backup_fixtures.asgi_call(app, "GET", f"/internal/backups/{artifact_id}")
        self.assertEqual(status, 200)
        library = next(item for item in scheduler.library()["artifacts"] if item["id"] == artifact_id)
        return library, listed, json.loads(body)

    def _assert_presented(self, scheduler, artifact_id, upload_check, *, passed):
        for view in self._views(scheduler, artifact_id):
            self.assertEqual(view["upload_check"], upload_check)
            self.assertIs(view["verified"], True)  # the catalogue flag keeps its restore and retention meaning
            if passed:
                self.assertIs(view["last_verify"]["ok"], True)
            else:
                self.assertIsNone(view["last_verify"])

    def _copies(self, scheduler):
        return {r.location: r for r in scheduler.catalog.list()}

    def test_a_matching_etag_is_recorded_as_a_provider_checksum_with_no_download(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        cloud = self._copies(scheduler)["cloud"]
        self.assertEqual(self.client.get_calls, 0)  # nobody read this copy back
        self.assertTrue(cloud.verified)
        self.assertEqual(scheduler._metadata_snapshot(cloud.artifact_id)["upload_check"], "provider_checksum")
        self._assert_presented(scheduler, cloud.artifact_id, "provider_checksum", passed=False)
        # Its own Verify downloads and hashes it; only then does it count as read back.
        self.assertTrue(scheduler.verify(cloud.artifact_id)["ok"])
        self.assertEqual(self.client.get_calls, 1)
        self._assert_presented(scheduler, cloud.artifact_id, "provider_checksum", passed=True)
        # Control: the filesystem copy of the same run was read back at upload.
        self._assert_presented(scheduler, self._copies(scheduler)["nas"].artifact_id, "readback", passed=False)

    def test_an_opaque_etag_is_read_back_once_and_described_without_overclaiming(self) -> None:
        self.client.etag_override = "kms-opaque"
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        cloud = self._copies(scheduler)["cloud"]
        self.assertEqual(self.client.get_calls, 1)
        self.assertEqual(scheduler._metadata_snapshot(cloud.artifact_id)["upload_check"], "readback")
        # A stored S3 "readback" cannot be told apart from one an earlier
        # release wrote for a matching ETag, so it is presented as either.
        self._assert_presented(scheduler, cloud.artifact_id, EITHER, passed=False)

    def test_an_earlier_release_s3_readback_record_is_presented_as_either_until_verified(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        cloud = self._copies(scheduler)["cloud"]
        with scheduler._state_lock:
            # What earlier releases stored for a matching ETag.
            scheduler._meta[cloud.artifact_id]["upload_check"] = "readback"
            scheduler._save_meta()
        scheduler.close()
        state = (self._paths.state_dir / "artifact-meta.json", self._paths.state_dir / "catalog.sqlite3")
        before = [path.read_bytes() for path in state]

        scheduler = self.make(self._config())  # restart on the new code
        self._assert_presented(scheduler, cloud.artifact_id, EITHER, passed=False)
        self.assertEqual(self.client.get_calls, 0)  # listing reads nothing back
        self.assertEqual([path.read_bytes() for path in state], before)  # and rewrites nothing
        # Control: a filesystem copy read back at upload keeps its readback.
        self._assert_presented(scheduler, self._copies(scheduler)["nas"].artifact_id, "readback", passed=False)
        self.assertTrue(scheduler.verify(cloud.artifact_id)["ok"])
        self.assertEqual(self.client.get_calls, 1)
        self._assert_presented(scheduler, cloud.artifact_id, EITHER, passed=True)
        scheduler.close()

        # A copy whose target is no longer configured cannot be placed either.
        scheduler = self.make(self._config(with_s3=False))
        with scheduler._state_lock:
            scheduler._meta[cloud.artifact_id].pop("last_verify")
        self._assert_presented(scheduler, cloud.artifact_id, EITHER, passed=False)

    def test_the_target_test_does_not_call_a_provider_checksum_a_readback(self) -> None:
        with self.open_target(SimpleSettings("s3")) as target:
            result = target.test()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["upload_check"], "provider_checksum")
        self.assertNotIn("readback matched", result["detail"])
        self.assertIn("not read back", result["detail"])
        self.assertEqual(self.client.get_calls, 0)
        # Control: an opaque ETag is downloaded and hashed, a real readback.
        self.client.etag_override = "kms-opaque"
        with self.open_target(SimpleSettings("s3")) as target:
            result = target.test()
        self.assertEqual(result["upload_check"], "readback")
        self.assertIn("readback matched", result["detail"])
        self.assertEqual(self.client.get_calls, 1)


@dataclasses.dataclass(frozen=True)
class SimpleSettings:
    provider: str
    target_id: str = "cloud"


class CleanupRefusalWordingTests(backup_fixtures.SchedulerTestBase):
    """The clean-up refusal says what its guard checks, not that a copy was read back."""

    def setUp(self) -> None:
        super().setUp()
        self.ftp = GatedFTP()
        self.ftp.release.set()

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            yield FtpTarget(self.ftp, "/backups", encrypted=False)

        return opened()

    def test_a_size_only_copy_kept_in_a_race_and_the_guard_asked_directly_is_not_called_verified(self) -> None:
        from datetime import timedelta

        from history_service.backup_scheduler.api import build_app

        config = {"full": {"enabled": True, "remote_keep": 2}, "targets": [backup_fixtures.TARGET]}
        scheduler = self.make(config)
        scheduler.run_now("full")
        self.now += timedelta(days=1)
        scheduler.run_now("full")
        older, newer = scheduler.catalog.list(location="nas")
        scheduler.close()
        scheduler = self.make({**config, "full": {"enabled": True, "remote_keep": 1}})
        app = build_app(scheduler)

        def call(method, path, body=None):
            status, payload = backup_fixtures.asgi_call(app, method, path, body)
            self.assertEqual(status, 200, payload)
            return json.loads(payload)

        plan = call("GET", "/internal/backups/lifecycle/plan")
        self.assertEqual([item["id"] for item in plan["items"]], [older.artifact_id])
        # The newer copy is damaged; its own Verify finds it and clears its flag,
        # so the older size-only copy becomes the newest one the guard protects.
        key = "/backups/" + newer.name
        self.ftp.files[key] = b"X" * len(self.ftp.files[key])
        self.assertIs(call("POST", f"/internal/backups/{newer.artifact_id}/verify", {})["ok"], False)
        reads, files = self.ftp.retr_calls, dict(self.ftp.files)
        # Through the admin route, which hands the page the scheduler's answer.
        status, result = _admin_call(app, "/api/admin/backups/lifecycle/apply", None, {"plan_token": plan["plan_token"]})
        self.assertEqual(status, 200)
        self.assertEqual(
            set(result),
            {"ok", "deleted", "already_missing", "failed", "not_attempted", "downgraded", "kept_after_replan"},
        )
        # No copy that passed a check would stay, so the clean-up keeps the
        # planned copy before the guard is asked (#1188).
        self.assertIs(result["ok"], True)
        self.assertEqual((result["deleted"], result["failed"]), ([], None))
        self.assertEqual(result["kept_after_replan"], [older.artifact_id])
        self.assertEqual((self.ftp.files, self.ftp.retr_calls), (files, reads))  # nothing deleted, nothing read
        row = scheduler.serialize(scheduler.get(older.artifact_id))
        self.assertEqual((row["upload_check"], row["last_verify"], row["verified"]), ("size", None, True))
        # The guard itself, asked about the same size-only copy, says what it checked.
        with self.assertRaises(CatalogError) as refused:
            scheduler.catalog.claim_deletion(scheduler.catalog.get(older.artifact_id), actor="op")
        self.assertEqual(str(refused.exception), GUARD_REFUSAL)
        self.assertNotIn("verified", str(refused.exception))
        self.assertEqual(self.ftp.files, files)

    def test_the_clean_up_route_returns_the_guards_refusal_for_a_size_only_copy(self) -> None:
        from datetime import timedelta

        from history_service.backup_scheduler.api import build_app

        config = {"full": {"enabled": True, "remote_keep": 3}, "targets": [backup_fixtures.TARGET]}
        scheduler = self.make(config)
        for _ in range(3):
            scheduler.run_now("full")
            self.now += timedelta(days=1)
        oldest, middle, newest = scheduler.catalog.list(location="nas")
        # A preserved copy stays whatever the plan says, so a checked copy is
        # left to read back and the clean-up goes on to ask the guard.
        scheduler.preserve(oldest.artifact_id, reason="synthetic retained copy", actor="op")
        scheduler.close()
        scheduler = self.make({**config, "full": {"enabled": True, "remote_keep": 1}})
        app = build_app(scheduler)

        def call(method, path, body=None):
            status, payload = backup_fixtures.asgi_call(app, method, path, body)
            self.assertEqual(status, 200, payload)
            return json.loads(payload)

        plan = call("GET", "/internal/backups/lifecycle/plan")
        self.assertEqual([item["id"] for item in plan["items"]], [middle.artifact_id])
        # The newest copy is damaged and its own Verify clears its flag, so the
        # planned middle copy becomes the newest one the guard protects.
        key = "/backups/" + newest.name
        self.ftp.files[key] = b"X" * len(self.ftp.files[key])
        self.assertIs(call("POST", f"/internal/backups/{newest.artifact_id}/verify", {})["ok"], False)
        for artifact in (oldest, middle):
            row = scheduler.serialize(scheduler.get(artifact.artifact_id))
            self.assertEqual((row["upload_check"], row["last_verify"], row["verified"]), ("size", None, True))
        reads, files = self.ftp.retr_calls, dict(self.ftp.files)

        status, result = _admin_call(app, "/api/admin/backups/lifecycle/apply", None, {"plan_token": plan["plan_token"]})

        self.assertEqual(status, 200)
        self.assertEqual(
            result,
            {
                "ok": False,
                "deleted": [],
                "already_missing": [],
                "failed": {"id": middle.artifact_id, "error": "CatalogError: " + GUARD_REFUSAL},
                "not_attempted": [],
                "downgraded": [],
                "kept_after_replan": [],
            },
        )
        self.assertNotIn("verified", result["failed"]["error"])
        self.assertEqual(self.ftp.files, files)  # nothing deleted
        self.assertEqual(self.ftp.retr_calls, reads + 1)  # the preserved keeper, read back once

    def test_the_wiki_does_not_call_the_copy_read_before_a_clean_up_verified(self) -> None:
        from pathlib import Path

        page = (Path(__file__).resolve().parents[1] / "wiki" / "Backup-Restore-and-Debug-Bundles.md").read_text(
            encoding="utf-8"
        )
        # The copy grooming reads back is chosen by its catalogue flag, which a
        # size-only or provider-checksum upload also sets.
        self.assertNotIn("newest verified", " ".join(page.split()))
        self.assertIn("passed its last completed check", " ".join(page.split()))


GUARD_REFUSAL = (
    "Artifact is the newest copy of its class at this location that passed its last completed check "
    "(the upload check or a later Verify), so it is kept."
)


def _admin_call(scheduler_app, route_path, artifact_id, body=None):
    """Call the admin route with its scheduler client bridged to the real scheduler app."""

    import asyncio
    from unittest.mock import patch

    from fastapi import HTTPException

    import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
    from admin_service.main import app as admin_app
    from admin_service.services.backup_scheduler_client import SchedulerResponse

    class Bridge:
        def request(self, method, path, *, body=None, actor=None, **_options):
            status, content = backup_fixtures.asgi_call(scheduler_app, method, path, body)
            return SchedulerResponse(status, json.loads(content))

    endpoint = next(route.endpoint for route in admin_app.routes if getattr(route, "path", "") == route_path)
    args = (artifact_id,) if artifact_id is not None else (body,)
    with patch("admin_service.routes.get_backup_scheduler_client", return_value=Bridge()):
        try:
            response = asyncio.run(endpoint(*args))
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}
    return response.status_code, json.loads(response.body)


class VerifyFailureResponseShapeTests(backup_fixtures.SchedulerTestBase):
    """What a failed Verify really returns: the page is tested against exactly this."""

    def setUp(self) -> None:
        super().setUp()
        self.ftp = GatedFTP()
        self.ftp.release.set()

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            if settings.target_id in self.broken_targets:
                raise ConnectionRefusedError("connection refused by nas.example.test")
            yield FtpTarget(self.ftp, "/backups", encrypted=False)

        return opened()

    def test_unreachable_and_mismatch_are_http_200_with_the_operation_result(self) -> None:
        from history_service.backup_scheduler.api import build_app

        scheduler = self.make({"full": {"enabled": True}, "targets": [backup_fixtures.TARGET]})
        scheduler.run_now("full")
        remote = scheduler.catalog.list(location="nas")[0]
        app = build_app(scheduler)
        path = f"/internal/backups/{remote.artifact_id}/verify"
        cases = (
            ("unreachable", "ConnectionRefusedError", True, "not_read"),
            ("mismatch", "Readback does not match the catalogued size and SHA-256.", False, "mismatch"),
        )
        for case, detail, flag, outcome in cases:
            with self.subTest(case=case):
                if case == "unreachable":
                    self.broken_targets = {"nas"}
                else:
                    self.broken_targets = set()
                    key = "/backups/" + remote.name
                    self.ftp.files[key] = b"X" * len(self.ftp.files[key])
                status, body = backup_fixtures.asgi_call(app, "POST", path, {})
                payload = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(set(payload), {"ok", "outcome", "artifact", "detail"})
                self.assertIs(payload["ok"], False)
                self.assertEqual(payload["outcome"], outcome)
                self.assertEqual(payload["detail"], detail)
                self.assertIs(payload["artifact"]["verified"], flag)
                self.assertNotIn("nas.example.test", body.decode())
                admin_status, admin_payload = _admin_call(app, "/api/admin/backups/{artifact_id}/verify", remote.artifact_id)
                self.assertEqual(admin_status, 200)
                self.assertEqual(set(admin_payload), {"ok", "outcome", "artifact", "detail"})
                self.assertIs(admin_payload["ok"], False)
                self.assertEqual(admin_payload["detail"], detail)


class ReadFailsTarget(LocalDirectoryTarget):
    """Opens fine, then the read of the copy fails part way."""

    def get(self, name, local_path, *, limit=None):
        import errno

        local_path.write_bytes(b"partial")
        # The text is deliberately misleading: the outcome must not come from it.
        raise OSError(errno.EIO, "Readback does not match the catalogued size and SHA-256.")


class VerifyOutcomeTests(backup_fixtures.SchedulerTestBase):
    """A Verify says which of three things happened, decided by where it stopped.

    Read and matched; read and did not match; or the copy could not be opened
    or read, so nothing was checked and its catalogue status stays as it was.
    One copy of each kind: read back at upload, size only at upload, and the
    provider's checksum at upload.
    """

    KINDS = (("nas", "readback"), ("ftp", "size"), ("cloud", "provider_checksum"))

    def setUp(self) -> None:
        super().setUp()
        self.client = archive_transport_fixtures.FakeS3Client()
        self.read_fails: set[str] = set()

    _copies = ProviderChecksumIsNotAReadbackTests._copies

    def open_target(self, settings):
        if settings.provider == "s3" and settings.target_id not in self.broken_targets:
            return ProviderChecksumIsNotAReadbackTests.open_target(self, settings)

        @contextlib.contextmanager
        def opened():
            if settings.target_id in self.broken_targets:
                raise ConnectionRefusedError("connection refused by nas.example.test")
            root = self.remote_root / settings.target_id
            if settings.target_id in self.read_fails:
                yield ReadFailsTarget(root)
            elif settings.target_id == "ftp":
                yield SizeOnlyTarget(root)
            else:
                yield LocalDirectoryTarget(root)

        return opened()

    def _config(self, *, with_s3: bool = True):
        config = ProviderChecksumIsNotAReadbackTests._config(self, with_s3=with_s3)
        config["targets"].append({**backup_fixtures.TARGET, "target_id": "ftp", "label": "Size-only share", "root": "/unused-ftp"})
        return config

    def _verify(self, scheduler, artifact_id):
        """Verify through the scheduler's HTTP app and the admin route; both must agree."""

        from history_service.backup_scheduler.api import build_app

        app = build_app(scheduler)
        status, body = backup_fixtures.asgi_call(app, "POST", f"/internal/backups/{artifact_id}/verify", {})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertNotIn("nas.example.test", body.decode())
        self.assertEqual(set(payload), {"ok", "outcome", "artifact", "detail"})
        admin_status, admin_payload = _admin_call(app, "/api/admin/backups/{artifact_id}/verify", artifact_id)
        self.assertEqual(admin_status, 200)
        self.assertEqual(admin_payload["outcome"], payload["outcome"])
        self.assertEqual(admin_payload["ok"], payload["ok"])
        return payload

    def _detail(self, scheduler, artifact_id):
        from history_service.backup_scheduler.api import build_app

        status, body = backup_fixtures.asgi_call(build_app(scheduler), "GET", f"/internal/backups/{artifact_id}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def _corrupt(self, location, record):
        if location == "cloud":
            self.client.corrupt_get = True
        else:
            path = self.remote_root / location / record.name
            path.write_bytes(b"X" * len(path.read_bytes()))

    def test_each_outcome_for_each_kind_of_copy(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        copies = self._copies(scheduler)
        for location, upload_check in self.KINDS:
            with self.subTest(copy=upload_check):
                record = copies[location]
                self.assertEqual(scheduler._metadata_snapshot(record.artifact_id)["upload_check"], upload_check)

                # Read and matched.
                passed = self._verify(scheduler, record.artifact_id)
                self.assertEqual((passed["ok"], passed["outcome"]), (True, "matched"))
                self.assertEqual(self._detail(scheduler, record.artifact_id)["last_verify"]["outcome"], "matched")

                # Could not be read: nothing checked, catalogue status unchanged.
                before = self._detail(scheduler, record.artifact_id)
                gets = self.client.get_calls
                self.broken_targets = {location}
                unread = self._verify(scheduler, record.artifact_id)
                self.broken_targets = set()
                self.assertEqual((unread["ok"], unread["outcome"]), (False, "not_read"))
                self.assertEqual(unread["detail"], "ConnectionRefusedError")
                self.assertEqual(self.client.get_calls, gets)  # nothing was read
                self.assertTrue(scheduler.get(record.artifact_id).verified)
                after = self._detail(scheduler, record.artifact_id)
                for key in ("verified", "restorable", "state", "upload_check"):
                    self.assertEqual(after[key], before[key], key)
                self.assertEqual(
                    after["last_verify"],
                    {"at": after["last_verify"]["at"], "ok": False, "outcome": "not_read", "detail": "ConnectionRefusedError"},
                )

                # Read and did not match: the flag is cleared, as before.
                self._corrupt(location, record)
                mismatch = self._verify(scheduler, record.artifact_id)
                self.client.corrupt_get = False
                self.assertEqual((mismatch["ok"], mismatch["outcome"]), (False, "mismatch"))
                self.assertEqual(mismatch["detail"], "Readback does not match the catalogued size and SHA-256.")
                self.assertIs(mismatch["artifact"]["verified"], False)
                self.assertEqual(self._detail(scheduler, record.artifact_id)["last_verify"]["outcome"], "mismatch")

    def test_a_read_that_fails_part_way_checked_nothing(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        self.read_fails = {"nas"}
        result = self._verify(scheduler, nas.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
        self.assertEqual(result["detail"], "OSError: Input/output error")
        self.assertTrue(scheduler.get(nas.artifact_id).verified)

    def test_a_local_copy_that_cannot_be_opened_is_not_read_and_a_missing_one_fails(self) -> None:
        from unittest import mock

        scheduler = self.make(self._config())
        local = scheduler.run_now("full")
        with mock.patch.object(scheduler, "_local_path", side_effect=PermissionError(13, "denied")):
            result = self._verify(scheduler, local.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
        self.assertTrue(scheduler.get(local.artifact_id).verified)
        # A local copy that is gone is a completed check with a negative result,
        # and clears the flag as it did before.
        scheduler._local_path(local).unlink()
        result = self._verify(scheduler, local.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (False, "mismatch"))
        self.assertFalse(scheduler.get(local.artifact_id).verified)

    def test_a_failure_after_the_comparison_is_not_called_unread(self) -> None:
        from unittest import mock

        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        with mock.patch.object(scheduler.catalog, "mark_verified", side_effect=RuntimeError("busy")):
            result = self._verify(scheduler, nas.artifact_id)
        # The copy was read and matched, but the result was not recorded: no outcome is claimed.
        self.assertIs(result["ok"], False)
        self.assertIsNone(result["outcome"])
        self.assertIsNone(self._detail(scheduler, nas.artifact_id)["last_verify"]["outcome"])

    def test_a_stored_outcome_is_shown_only_when_it_is_well_formed(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        cases = (
            ({"ok": False, "detail": "x"}, None),  # earlier releases: no outcome, today's wording
            ({"ok": False, "outcome": "not_read", "detail": "x"}, "not_read"),
            ({"ok": True, "outcome": "not_read", "detail": "x"}, None),
            ({"ok": False, "outcome": "matched", "detail": "x"}, None),
            ({"ok": True, "outcome": "matched", "detail": "x"}, "matched"),
            ({"ok": False, "outcome": ["not_read"], "detail": "x"}, None),
            ({"ok": False, "outcome": "unread", "detail": "x"}, None),
        )
        for stored, presented in cases:
            with self.subTest(stored=stored):
                with scheduler._state_lock:
                    scheduler._meta[nas.artifact_id]["last_verify"] = {
                        "at": "2026-09-24T12:00:00+00:00", "artifact_id": nas.artifact_id, **stored,
                    }
                last_verify = self._detail(scheduler, nas.artifact_id)["last_verify"]
                self.assertIn("outcome", last_verify, "the details carry no Verify outcome")
                self.assertEqual(last_verify["outcome"], presented)

    def test_a_rejection_the_caller_is_already_handling_says_nothing_about_another_copy(self) -> None:
        from history_service.backup_scheduler.service import ArchiveIntegrityError

        scheduler = self.make(self._config())
        scheduler.run_now("full")
        copies = self._copies(scheduler)
        local, remote = copies["local"], copies["nas"]
        path = scheduler._local_path(local)
        path.write_bytes(b"X" * len(path.read_bytes()))
        self.broken_targets = {"nas"}
        try:
            with scheduler.materialize(local.artifact_id):
                self.fail("the altered local copy passed its check")
        except ArchiveIntegrityError:
            # The remote copy cannot be opened, so none of it is read; the
            # rejection being handled here is the local copy's.
            result = scheduler.verify(remote.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
        self.assertEqual(result["detail"], "ConnectionRefusedError")
        self.assertIs(result["artifact"]["verified"], True)
        self.assertIs(result["artifact"]["restorable"], True)
        self.assertTrue(scheduler.get(remote.artifact_id).verified)
        last = self._detail(scheduler, remote.artifact_id)["last_verify"]
        self.assertEqual((last["ok"], last["outcome"]), (False, "not_read"))


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
class VerifyCleanupFailureTests(backup_fixtures.SchedulerTestBase):
    """Removing the fetched copy can fail after the comparison; the comparison result stands.

    The failure is real: right after the unchanged comparison, the task's own
    fetch directory is made read-only, so removing the fetched file fails at
    the operating system with ``PermissionError``.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client = archive_transport_fixtures.FakeS3Client()
        self.read_fails: set[str] = set()

    _copies = VerifyOutcomeTests._copies
    open_target = VerifyOutcomeTests.open_target
    _config = VerifyOutcomeTests._config
    _verify = VerifyOutcomeTests._verify
    _detail = VerifyOutcomeTests._detail
    _corrupt = VerifyOutcomeTests._corrupt

    @contextlib.contextmanager
    def _errors_logged(self):
        import logging

        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(f"{record.levelname}:{record.getMessage()}")

        handler = Capture(logging.ERROR)
        log = logging.getLogger("history_service.backup_scheduler.service")
        log.addHandler(handler)
        try:
            yield records
        finally:
            log.removeHandler(handler)

    def _lock_fetch_dir_after_comparison(self, scheduler):
        from unittest import mock

        from history_service.backup_scheduler import service

        original = service._require_match
        locked = []

        def compare_then_lock(*args):
            try:
                original(*args)
            finally:
                for workspace in scheduler.paths.state_dir.glob("backup-fetch-*"):
                    workspace.chmod(0o500)
                    locked.append(workspace)

        def unlock():
            for workspace in locked:
                workspace.chmod(0o700)

        self.addCleanup(unlock)
        return mock.patch.object(service, "_require_match", side_effect=compare_then_lock)

    def test_a_mismatch_stays_a_mismatch_when_clean_up_fails(self) -> None:
        from history_service.backup_scheduler.api import build_app

        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        self._corrupt("nas", nas)
        with self._lock_fetch_dir_after_comparison(scheduler), self._errors_logged() as logs:
            result = self._verify(scheduler, nas.artifact_id)
            status, body = backup_fixtures.asgi_call(
                build_app(scheduler), "GET", f"/internal/backups/{nas.artifact_id}/download"
            )
        self.assertEqual((result["ok"], result["outcome"]), (False, "mismatch"))
        self.assertEqual(result["detail"], "Readback does not match the catalogued size and SHA-256.")
        self.assertIs(result["artifact"]["verified"], False)
        self.assertIs(result["artifact"]["restorable"], False)
        self.assertFalse(scheduler.get(nas.artifact_id).verified)
        self.assertEqual(self._detail(scheduler, nas.artifact_id)["last_verify"]["outcome"], "mismatch")
        # A download of the same bytes is refused as a mismatch, not as an unreadable copy.
        self.assertEqual(status, 409, body)
        self.assertTrue(logs and all(line.startswith("ERROR:") and "PermissionError" in line for line in logs), logs)

    def test_a_match_stays_a_match_when_clean_up_fails(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        with self._lock_fetch_dir_after_comparison(scheduler), self._errors_logged() as logs:
            result = self._verify(scheduler, nas.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))
        self.assertTrue(scheduler.get(nas.artifact_id).verified)
        self.assertEqual(self._detail(scheduler, nas.artifact_id)["last_verify"]["outcome"], "matched")
        self.assertTrue(logs and all(line.startswith("ERROR:") and "PermissionError" in line for line in logs), logs)
        self.assertFalse(any(str(scheduler.paths.state_dir) in line for line in logs), logs)

    def test_an_ordinary_mismatch_is_still_a_mismatch(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        self._corrupt("nas", nas)
        result = self._verify(scheduler, nas.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (False, "mismatch"))
        self.assertFalse(scheduler.get(nas.artifact_id).verified)
        self.assertEqual(list(scheduler.paths.state_dir.glob("backup-fetch-*")), [])


class RejectedCopyStaysRejectedTests(backup_fixtures.SchedulerTestBase):
    """A size or content check that rejected the copy stays the result, whatever fails afterwards.

    The copy is read through the shipping SFTP and S3 transport contexts (only
    the network clients are fakes) or through the shipping download
    destination, and is longer than catalogued, so the shipping size guard rejects
    it. A later step then fails: closing the SFTP session, closing the SSH
    client, closing the S3 client, or removing the partial download. No file
    permissions are involved, so these tests also run as root.
    """

    SIZE_REJECTED = "Archive download is larger than expected; stopped before the catalogue check."
    SITES = ("sftp_session_close", "ssh_client_close", "s3_client_close", "download_unlink")

    def setUp(self) -> None:
        super().setUp()
        self.site: str | None = None
        self.failure: BaseException | None = None
        self.stored = b""
        self.read_error: BaseException | None = None

    _verify = VerifyOutcomeTests._verify
    _detail = VerifyOutcomeTests._detail

    def _fail(self, *args, **kwargs):
        raise self.failure

    def open_target(self, settings):
        from types import SimpleNamespace
        from unittest import mock

        from history_service.backup_archive import transport

        if self.site is None:
            return super().open_target(settings)
        stack = contextlib.ExitStack()
        if self.site in {"sftp_session_close", "ssh_client_close"}:
            ssh = archive_transport_fixtures.FakeSSHClient()
            ssh.connect = lambda **kwargs: None
            ssh.sftp.dirs.add("/backups")
            for name in self.names:
                ssh.sftp.files["/backups/" + name] = self.stored
            if self.read_error is not None:
                def open_fails(*args):
                    raise self.read_error

                ssh.sftp.open = open_fails
            if self.site == "sftp_session_close":
                ssh.sftp.close = self._fail
            else:
                ssh.close = self._fail
            stack.enter_context(mock.patch.object(transport.paramiko, "SSHClient", return_value=ssh))
            stack.enter_context(mock.patch.object(transport, "_sftp_host_key_policy", return_value=None))
            stack.enter_context(mock.patch.object(transport, "_open_sftp_session", return_value=ssh.sftp))
            sftp = SimpleNamespace(
                password_file=None, private_key_file=None, hostname="sftp.example.test", effective_port=22,
                username="backup", timeout_seconds=1, root="/backups",
            )
            opened = transport._open_sftp(sftp)
        elif self.site == "s3_client_close":
            client = archive_transport_fixtures.FakeS3Client()
            for name in self.names:
                client.objects["backups/" + name] = {"data": self.stored}
            client.close = self._fail
            boto3 = SimpleNamespace(client=lambda *args, **kwargs: client)
            stack.enter_context(mock.patch.object(
                transport, "_import_boto3",
                return_value=(boto3, archive_transport_fixtures._TransferConfig, lambda **kwargs: None, ()),
            ))
            stack.enter_context(mock.patch.object(transport, "read_secret_file", return_value="synthetic"))
            s3 = SimpleNamespace(
                access_key_id_file="id", secret_access_key_file="key", timeout_seconds=1, endpoint_url="",
                region="", bucket="backups", root="backups",
            )
            opened = transport._open_s3(s3)
        else:
            test = self

            class Remote:
                def get(self, name, local_path, *, limit=None):
                    real_unlink = os.unlink
                    calls = []

                    def unlink(path, *args, **kwargs):
                        calls.append(path)
                        if len(calls) == 1:  # the download destination's own removal
                            raise test.failure
                        return real_unlink(path, *args, **kwargs)

                    import io

                    with mock.patch.object(transport.os, "unlink", side_effect=unlink):
                        with transport._download_destination(local_path) as target:
                            return transport._copy_stream(io.BytesIO(test.stored), target.write, limit)

            opened = contextlib.nullcontext(Remote())

        @contextlib.contextmanager
        def with_fakes():
            with stack, opened as remote:
                yield remote

        return with_fakes()

    def _scheduler(self):
        scheduler = self.make({"full": {"enabled": True}, "targets": [backup_fixtures.TARGET]})
        scheduler.run_now("full")
        remote = scheduler.catalog.list(location="nas")[0]
        self.names = [remote.name]
        stored = (self.remote_root / "nas" / remote.name).read_bytes()
        self.assertTrue(scheduler.get(remote.artifact_id).verified)
        return scheduler, remote, stored

    def _download(self, scheduler, artifact_id):
        from history_service.backup_scheduler.api import build_app

        status, body = backup_fixtures.asgi_call(build_app(scheduler), "GET", f"/internal/backups/{artifact_id}/download")
        return status, json.loads(body) if status != 200 else None

    def test_a_size_rejection_stays_a_mismatch_when_a_later_step_fails(self) -> None:
        import errno

        for site in self.SITES:
            for failure in (OSError(errno.EIO, "Input/output error"), PermissionError(errno.EACCES, "Permission denied"), RuntimeError("closed")):
                with self.subTest(site=site, failure=type(failure).__name__):
                    scheduler, remote, stored = self._scheduler()
                    self.stored = stored + b"OVERSIZED"
                    self.site, self.failure = site, failure
                    result = self._verify(scheduler, remote.artifact_id)
                    self.assertEqual((result["ok"], result["outcome"]), (False, "mismatch"))
                    self.assertEqual(result["detail"], self.SIZE_REJECTED)
                    self.assertIs(result["artifact"]["verified"], False)
                    self.assertIs(result["artifact"]["restorable"], False)
                    self.assertFalse(scheduler.get(remote.artifact_id).verified)
                    last = self._detail(scheduler, remote.artifact_id)["last_verify"]
                    self.assertEqual((last["ok"], last["outcome"], last["detail"]), (False, "mismatch", self.SIZE_REJECTED))
                    # A download of the same copy is refused as a mismatch too.
                    status, body = self._download(scheduler, remote.artifact_id)
                    self.assertEqual(status, 409, body)
                    self.assertEqual(body["detail"], f"{self.SIZE_REJECTED} Verify it, or use another copy.")
                    self.site = None
                    self.doCleanups()
                    self.setUp()

    def test_a_non_os_error_from_workspace_removal_after_a_mismatch_is_a_mismatch(self) -> None:
        from pathlib import Path
        from unittest import mock

        scheduler, remote, stored = self._scheduler()
        (self.remote_root / "nas" / remote.name).write_bytes(b"X" * len(stored))
        with mock.patch.object(Path, "rmdir", side_effect=RuntimeError("removal failed")):
            result = self._verify(scheduler, remote.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (False, "mismatch"))
        self.assertEqual(result["detail"], "Readback does not match the catalogued size and SHA-256.")
        self.assertFalse(scheduler.get(remote.artifact_id).verified)

    def test_a_later_failure_without_a_rejection_is_still_unread(self) -> None:
        import errno

        for site in self.SITES[:3]:
            with self.subTest(site=site):
                scheduler, remote, stored = self._scheduler()
                # The copy is intact; only closing fails, so the comparison never ran.
                self.stored, self.site, self.failure = stored, site, OSError(errno.EIO, "Input/output error")
                result = self._verify(scheduler, remote.artifact_id)
                self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
                self.assertEqual(result["detail"], "OSError: Input/output error")
                self.assertTrue(scheduler.get(remote.artifact_id).verified)
                status, _body = self._download(scheduler, remote.artifact_id)
                self.assertEqual(status, 502)
                self.site = None
                self.doCleanups()
                self.setUp()

    def test_a_read_failure_before_any_check_then_a_close_failure_is_unread(self) -> None:
        import errno

        scheduler, remote, stored = self._scheduler()
        self.stored, self.site = stored + b"OVERSIZED", "sftp_session_close"
        self.read_error = ConnectionResetError(errno.ECONNRESET, "Connection reset")
        self.failure = OSError(errno.EIO, "Input/output error")
        result = self._verify(scheduler, remote.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
        self.assertTrue(scheduler.get(remote.artifact_id).verified)

    def test_a_match_then_a_workspace_removal_os_error_is_still_a_match(self) -> None:
        from pathlib import Path
        from unittest import mock

        scheduler, remote, _stored = self._scheduler()
        with mock.patch.object(Path, "rmdir", side_effect=OSError(16, "Device or resource busy")):
            result = self._verify(scheduler, remote.artifact_id)
        self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))
        self.assertTrue(scheduler.get(remote.artifact_id).verified)

    def test_interruption_and_cancellation_from_clean_up_propagate(self) -> None:
        import asyncio

        for failure in (KeyboardInterrupt(), asyncio.CancelledError()):
            for site in ("sftp_session_close", "download_unlink"):
                with self.subTest(site=site, failure=type(failure).__name__):
                    scheduler, remote, stored = self._scheduler()
                    self.stored, self.site, self.failure = stored + b"OVERSIZED", site, failure
                    with self.assertRaises(type(failure)):
                        scheduler.verify(remote.artifact_id)
                    self.site = None
                    self.doCleanups()
                    self.setUp()

    def test_only_a_rejection_raised_during_this_verify_counts(self) -> None:
        import errno

        from history_service.backup_scheduler.service import ArchiveIntegrityError

        for stored_suffix, expected in ((b"OVERSIZED", "mismatch"), (b"", "not_read")):
            with self.subTest(expected=expected):
                scheduler, remote, stored = self._scheduler()
                self.stored, self.site = stored + stored_suffix, "sftp_session_close"
                self.failure = OSError(errno.EIO, "Input/output error")
                try:
                    raise ArchiveIntegrityError("Readback of another copy does not match.")
                except ArchiveIntegrityError:
                    result = scheduler.verify(remote.artifact_id)
                    status, _body = self._download(scheduler, remote.artifact_id)
                self.assertEqual((result["ok"], result["outcome"]), (False, expected))
                self.assertEqual(status, 409 if expected == "mismatch" else 502)
                if expected == "mismatch":
                    # This copy's own rejection, hidden by the failing close, still counts.
                    self.assertEqual(result["detail"], self.SIZE_REJECTED)
                    self.assertFalse(scheduler.get(remote.artifact_id).verified)
                else:
                    # Intact copy, failing close: the caller's rejection is not about it.
                    self.assertEqual(result["detail"], "OSError: Input/output error")
                    self.assertTrue(scheduler.get(remote.artifact_id).verified)
                self.site = None
                self.doCleanups()
                self.setUp()

    def test_the_chain_walk_stops_at_what_the_caller_was_handling(self) -> None:
        from history_service.backup_archive.transport import ArchiveDownloadTooLargeError
        from history_service.backup_scheduler.service import ArchiveIntegrityError, rejected_copy_error

        def walk(exc, ambient):
            try:
                return rejected_copy_error(exc, ambient)
            except TypeError:
                self.fail("the walk cannot be told what the caller was already handling")

        ambient = ArchiveIntegrityError("Readback of another copy does not match.")
        own = ArchiveDownloadTooLargeError(self.SIZE_REJECTED)
        behind = ArchiveIntegrityError("older still")
        ambient.__context__ = behind
        close_failed = OSError("close")
        close_failed.__context__ = ambient
        self.assertIsNone(walk(close_failed, ambient))
        self.assertIsNone(walk(ambient, ambient))
        # This attempt's own rejection is found, in front of the caller's.
        own.__context__ = ambient
        close_failed.__context__ = own
        self.assertIs(walk(close_failed, ambient), own)
        # Stopping at the caller's exception does not end the walk elsewhere.
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__, wrapped.__context__ = own, ambient
        self.assertIs(walk(wrapped, ambient), own)
        # Without a caller exception the walk is as before.
        self.assertIs(walk(close_failed, None), own)
        self.assertIs(rejected_copy_error(close_failed), own)

    def test_the_chain_walk_is_bounded_and_survives_a_cycle(self) -> None:
        from history_service.backup_archive.transport import ArchiveDownloadTooLargeError
        from history_service.backup_scheduler import service
        from history_service.backup_scheduler.service import ArchiveIntegrityError

        rejected_copy_error = getattr(service, "rejected_copy_error", None)
        self.assertIsNotNone(rejected_copy_error, "verify has no exception-chain walk")

        def chain(length, last):
            head = current = OSError("cleanup")
            for _ in range(length):
                current.__context__ = OSError("cleanup")
                current = current.__context__
            current.__context__ = last
            return head

        size = ArchiveDownloadTooLargeError(self.SIZE_REJECTED)
        content = ArchiveIntegrityError("Readback does not match the catalogued size and SHA-256.")
        self.assertIs(rejected_copy_error(chain(2, size)), size)
        cause = RuntimeError("wrapped")
        cause.__cause__ = content
        self.assertIs(rejected_copy_error(cause), content)
        # Beyond the bound the walk stops and finds nothing.
        self.assertIsNone(rejected_copy_error(chain(10_000, size)))
        # The bound is 32 exceptions examined, counting the one raised and the rejection.
        self.assertIs(rejected_copy_error(chain(30, size)), size)
        self.assertIsNone(rejected_copy_error(chain(31, size)))
        # A later failure raised "from None" still has the rejection behind it.
        hidden = OSError("close")
        hidden.__context__, hidden.__suppress_context__ = size, True
        self.assertIs(rejected_copy_error(hidden), size)
        # Two rejections behind one failure: one of them is found (which one is not promised).
        both = RuntimeError("wrapped")
        both.__cause__, both.__context__ = content, size
        self.assertIn(rejected_copy_error(both), (content, size))
        first, second = OSError("a"), OSError("b")
        first.__context__, second.__context__ = second, first
        second.__cause__ = first
        self.assertIsNone(rejected_copy_error(first))
        self.assertIsNone(rejected_copy_error(OSError("alone")))


class UploadCheckIsReadFromTheCopyTests(backup_fixtures.SchedulerTestBase):
    """How a copy was checked at upload comes from that copy's record (#1288).

    Earlier releases stored "readback" for an S3 upload whose ETag matched, and
    recorded nothing about the provider. Changing the target's provider later
    must not turn that stored word into a readback nobody performed: only the
    copy's own successful Verify does that. The real S3 target over the
    maintained in-memory client; filesystem targets in the temporary directory.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client = archive_transport_fixtures.FakeS3Client()

    open_target = ProviderChecksumIsNotAReadbackTests.open_target
    _views = ProviderChecksumIsNotAReadbackTests._views
    _assert_presented = ProviderChecksumIsNotAReadbackTests._assert_presented
    _copies = ProviderChecksumIsNotAReadbackTests._copies

    def _config(self, *, cloud_provider: str = "s3", nas_provider: str = "filesystem"):
        config = ProviderChecksumIsNotAReadbackTests._config(self)
        s3, nas = config["targets"]
        cloud = s3
        if cloud_provider != "s3":
            cloud = {**backup_fixtures.TARGET, "target_id": "cloud", "label": "Cloud bucket", "provider": cloud_provider,
                     "root": "/unused-cloud"}
        if nas_provider == "s3":
            nas = {**s3, "target_id": "nas", "label": "Office NAS"}
        config["targets"] = [cloud, nas]
        return config

    def _earlier_release_copy(self):
        """An S3 copy as an earlier release left it: "readback" for a matching ETag, no provider."""

        scheduler = self.make(self._config())
        scheduler.run_now("full")
        cloud = self._copies(scheduler)["cloud"]
        self.assertEqual(self.client.get_calls, 0)  # the ETag matched; nothing was read back
        with scheduler._state_lock:
            meta = scheduler._meta[cloud.artifact_id]
            meta["upload_check"] = "readback"
            meta.pop("upload_provider", None)
            scheduler._save_meta()
        scheduler.close()
        return cloud

    def test_changing_the_targets_provider_does_not_relabel_an_old_upload_as_read_back(self) -> None:
        cloud = self._earlier_release_copy()
        scheduler = self.make(self._config())
        self._assert_presented(scheduler, cloud.artifact_id, EITHER, passed=False)
        scheduler.close()

        # Only the target's current provider changes; the copy and its record do not.
        scheduler = self.make(self._config(cloud_provider="filesystem"))
        self._assert_presented(scheduler, cloud.artifact_id, EITHER, passed=False)
        self.assertEqual(self.client.get_calls, 0)  # presenting the copy read nothing back

    def test_a_new_copy_records_the_provider_it_was_uploaded_with(self) -> None:
        self.client.etag_override = "kms-opaque"  # the S3 copy is read back at upload
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        copies = self._copies(scheduler)
        self.assertEqual(scheduler._metadata_snapshot(copies["cloud"].artifact_id).get("upload_provider"), "s3")
        self.assertEqual(scheduler._metadata_snapshot(copies["nas"].artifact_id).get("upload_provider"), "filesystem")
        self.assertIsNone(scheduler._metadata_snapshot(copies["local"].artifact_id).get("upload_provider"))
        scheduler.close()

        # Swapping both targets' providers changes neither copy's presentation.
        scheduler = self.make(self._config(cloud_provider="filesystem", nas_provider="s3"))
        self._assert_presented(scheduler, copies["cloud"].artifact_id, EITHER, passed=False)
        self._assert_presented(scheduler, copies["nas"].artifact_id, "readback", passed=False)

    def test_an_earlier_filesystem_readback_record_is_presented_as_either_until_verified(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        self._assert_presented(scheduler, nas.artifact_id, "readback", passed=False)  # control: recorded provider
        with scheduler._state_lock:
            scheduler._meta[nas.artifact_id].pop("upload_provider", None)
            scheduler._save_meta()
        scheduler.close()

        # The record no longer says which provider took the upload, so the
        # current filesystem target cannot vouch for it.
        scheduler = self.make(self._config())
        self._assert_presented(scheduler, nas.artifact_id, EITHER, passed=False)
        self.assertTrue(scheduler.verify(nas.artifact_id)["ok"])
        self._assert_presented(scheduler, nas.artifact_id, EITHER, passed=True)

    def test_a_malformed_recorded_provider_is_unknown(self) -> None:
        scheduler = self.make(self._config())
        scheduler.run_now("full")
        nas = self._copies(scheduler)["nas"]
        for value in ("", "tape", ["filesystem"], {"provider": "filesystem"}, None, 1):
            with self.subTest(value=value):
                with scheduler._state_lock:
                    scheduler._meta[nas.artifact_id]["upload_provider"] = value
                self._assert_presented(scheduler, nas.artifact_id, EITHER, passed=False)


if __name__ == "__main__":
    unittest.main()
