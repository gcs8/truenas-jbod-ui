"""A copy kept after an uncertain publication records the provider that took it (#1415).

When a filesystem target publishes the archive, reads it back and matches it,
but cannot confirm that the directory entry is durable, the scheduler keeps the
copy as preserved and unverified (#1288 follow-up). Its record must still say
which provider took the upload: the read-back presentation is read only from
that record, and a record that names no provider is presented as "provider
checksum or read-back" (#1288). Without the recorded provider, the preserved
copy would lose its truthful read-back presentation.

Synthetic local-directory targets in the temporary directory only; the failing
directory barrier is simulated, nothing leaves the test's temporary tree.
"""

from __future__ import annotations

import contextlib
import errno
import json
import unittest

from history_service.backup_archive.transport import LocalDirectoryTarget

import tests.test_backup_integration as backup_fixtures

UNCERTAIN_REASON = "Remote publication has uncertain directory durability; verify before releasing preservation."


class UncertainBarrierTarget(LocalDirectoryTarget):
    """The real filesystem upload and read-back, with a directory barrier that fails."""

    @staticmethod
    def _fsync_dir(path) -> None:
        raise OSError(errno.EIO, "synthetic directory barrier failure")


class UncertainPublicationRecordsItsProviderTests(backup_fixtures.SchedulerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.uncertain_targets: set[str] = {"nas"}

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            kind = UncertainBarrierTarget if settings.target_id in self.uncertain_targets else LocalDirectoryTarget
            yield kind(self.remote_root / settings.target_id)

        return opened()

    def _config(self, *, provider: str = "filesystem"):
        target = dict(backup_fixtures.TARGET)
        if provider == "s3":
            access, secret = self.root / "synthetic-access", self.root / "synthetic-secret"
            for path in (access, secret):
                path.write_text("synthetic-only")
                path.chmod(0o600)
            target.update(provider="s3", root="archive", bucket="synthetic",
                          access_key_id_file=str(access), secret_access_key_file=str(secret))
        return {"full": {"enabled": True}, "targets": [target]}

    def _uncertain_copy(self, scheduler):
        scheduler.run_now("full")
        (copy,) = scheduler.catalog.list(location="nas")
        return copy

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
        return {"library": library, "http list": listed, "http details": json.loads(body)}

    def _assert_presented(self, scheduler, artifact_id, *, verified, passed):
        for name, view in self._views(scheduler, artifact_id).items():
            with self.subTest(view=name):
                self.assertEqual(view["upload_check"], "readback")
                self.assertIs(view["verified"], verified)
                self.assertIs(view["preserved"], True)
                self.assertEqual(view["preserve_reason"], UNCERTAIN_REASON)
                if passed:
                    self.assertIs(view["last_verify"]["ok"], True)
                else:
                    self.assertIsNone(view["last_verify"])

    def test_the_preserved_copy_records_the_filesystem_provider_and_keeps_its_readback(self) -> None:
        scheduler = self.make(self._config())
        copy = self._uncertain_copy(scheduler)
        # The uncertain path was taken: the target failed, the copy is kept unverified.
        self.assertFalse(scheduler.library()["targets"][0]["last_run"]["ok"])
        self.assertTrue(copy.preserved)
        self.assertFalse(copy.verified)
        meta = scheduler._metadata_snapshot(copy.artifact_id)
        self.assertEqual(meta.get("upload_check"), "readback")
        self.assertEqual(meta.get("upload_provider"), "filesystem")
        self._assert_presented(scheduler, copy.artifact_id, verified=False, passed=False)

        # Its own Verify re-reads it; the recorded check is unchanged.
        self.uncertain_targets.clear()
        self.assertTrue(scheduler.verify(copy.artifact_id)["ok"])
        self._assert_presented(scheduler, copy.artifact_id, verified=True, passed=True)

    def test_the_recorded_provider_survives_a_restart_and_a_provider_change(self) -> None:
        scheduler = self.make(self._config())
        copy = self._uncertain_copy(scheduler)
        scheduler.close()

        # The record, not the target's current settings, decides the presentation.
        for provider in ("filesystem", "s3"):
            with self.subTest(current_provider=provider):
                scheduler = self.make(self._config(provider=provider))
                self.assertEqual(scheduler._metadata_snapshot(copy.artifact_id).get("upload_provider"), "filesystem")
                self._assert_presented(scheduler, copy.artifact_id, verified=False, passed=False)
                scheduler.close()

    def test_control_the_same_copy_without_a_recorded_provider_is_presented_as_either(self) -> None:
        scheduler = self.make(self._config())
        copy = self._uncertain_copy(scheduler)
        with scheduler._state_lock:
            scheduler._meta[copy.artifact_id].pop("upload_provider", None)
            scheduler._save_meta()
        for name, view in self._views(scheduler, copy.artifact_id).items():
            with self.subTest(view=name):
                self.assertEqual(view["upload_check"], "provider_checksum_or_readback")


if __name__ == "__main__":
    unittest.main()
