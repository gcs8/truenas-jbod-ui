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

    def test_a_size_only_copy_refused_in_a_race_is_not_called_verified(self) -> None:
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
        self.assertEqual(set(result), {"ok", "deleted", "already_missing", "failed", "not_attempted"})
        self.assertIs(result["ok"], False)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["failed"]["id"], older.artifact_id)
        self.assertEqual(result["failed"]["error"], f"CatalogError: {GUARD_REFUSAL}")
        self.assertNotIn("verified", result["failed"]["error"])
        self.assertEqual((self.ftp.files, self.ftp.retr_calls), (files, reads))  # nothing deleted, nothing read
        row = scheduler.serialize(scheduler.get(older.artifact_id))
        self.assertEqual((row["upload_check"], row["last_verify"], row["verified"]), ("size", None, True))


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
            ("unreachable", "ConnectionRefusedError", True),
            ("mismatch", "Readback does not match the catalogued size and SHA-256.", False),
        )
        for case, detail, flag in cases:
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
                self.assertEqual(set(payload), {"ok", "artifact", "detail"})
                self.assertIs(payload["ok"], False)
                self.assertEqual(payload["detail"], detail)
                self.assertIs(payload["artifact"]["verified"], flag)
                self.assertNotIn("nas.example.test", body.decode())
                admin_status, admin_payload = _admin_call(app, "/api/admin/backups/{artifact_id}/verify", remote.artifact_id)
                self.assertEqual(admin_status, 200)
                self.assertEqual(set(admin_payload), {"ok", "artifact", "detail"})
                self.assertIs(admin_payload["ok"], False)
                self.assertEqual(admin_payload["detail"], detail)


if __name__ == "__main__":
    unittest.main()
