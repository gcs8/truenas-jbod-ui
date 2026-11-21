"""Backup fetch workspaces have a bounded lifecycle after a failed clean-up (#1290).

Verify and download read a remote copy into a ``backup-fetch-*`` directory under
the scheduler's private state folder. When removing that directory fails, the
comparison result still stands (#1202), but the full copy stays. A later fetch
reclaims such a directory only when it is a plain directory owned by this
account, no live fetch holds it, and it holds nothing but the fetched file.
Anything else is left in place and reported, and once too many abandoned
directories are left a new fetch is refused with the count instead of adding
another full copy.

Synthetic files only: the remote target is the real filesystem transport over a
temporary directory, and clean-up failures are real permission denials.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import unittest
from pathlib import Path
from unittest import mock

from history_service.backup_scheduler import service
from tests.test_backup_integration import TARGET, SchedulerTestBase, asgi_call

# The bound on abandoned fetch directories left in place (each can hold a full
# copy). Written out here so a changed bound is a visible test edit.
ABANDONED_LIMIT = 4
NEEDS_PERMISSIONS = unittest.skipIf(
    hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions"
)


class FetchWorkspaceReclaimTests(SchedulerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.scheduler = self.make({"full": {"enabled": True, "local_keep": 1, "remote_keep": 1}, "targets": [TARGET]})
        self.scheduler.run_now("full")
        (self.remote,) = self.scheduler.catalog.list(location="nas")
        self.state_dir = self._paths.state_dir
        self.locked: list[Path] = []
        self.addCleanup(self._unlock)

    # -- helpers -----------------------------------------------------------------

    def workspaces(self) -> list[str]:
        return sorted(path.name for path in self.state_dir.glob("backup-fetch-*"))

    def _unlock(self) -> None:
        for path in self.locked:
            if path.exists() and not path.is_symlink():
                path.chmod(0o700)
        self.locked.clear()

    @contextlib.contextmanager
    def _clean_up_fails(self):
        """Make the fetch's own clean-up meet a real permission denial."""

        original = service._require_match

        def compare_then_lock(*args):
            try:
                original(*args)
            finally:
                for workspace in self.state_dir.glob("backup-fetch-*"):
                    if workspace.is_dir() and not workspace.is_symlink():
                        workspace.chmod(0o500)
                        self.locked.append(workspace)

        with mock.patch.object(service, "_require_match", side_effect=compare_then_lock):
            yield

    @contextlib.contextmanager
    def _logged(self):
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(f"{record.levelname}:{record.getMessage()}")

        handler = Capture(logging.WARNING)
        log = logging.getLogger("history_service.backup_scheduler.service")
        log.addHandler(handler)
        try:
            yield records
        finally:
            log.removeHandler(handler)

    def fetch(self) -> bytes:
        with self.scheduler.materialize(self.remote.artifact_id) as (_record, path):
            return path.read_bytes()

    def abandoned_workspace(self, name: str = "backup-fetch-abandoned", *, content: bytes = b"full copy") -> Path:
        workspace = self.state_dir / name
        workspace.mkdir(mode=0o700)
        (workspace / "archive").write_bytes(content)
        return workspace

    def unrecognised_workspace(self, name: str) -> Path:
        workspace = self.state_dir / name
        workspace.mkdir(mode=0o700)
        (workspace / "archive").write_bytes(b"full copy")
        (workspace / "notes.txt").write_bytes(b"not written by a fetch")
        return workspace

    def verify(self) -> dict:
        from history_service.backup_scheduler.api import build_app

        path = f"/internal/backups/{self.remote.artifact_id}/verify"
        status, body = asgi_call(build_app(self.scheduler), "POST", path, {})
        self.assertEqual(status, 200, body)
        return json.loads(body)

    # -- the issue's reproduction ------------------------------------------------

    @NEEDS_PERMISSIONS
    def test_failed_clean_ups_are_reclaimed_by_later_fetches(self) -> None:
        retained = []
        for _ in range(3):
            with self._clean_up_fails():
                self.assertEqual(hashlib.sha256(self.fetch()).hexdigest(), self.remote.sha256)
            self._unlock()  # normal permissions are back after the fetch returns
            retained.append(len(self.workspaces()))
        # Each fetch leaves only its own failed directory; earlier ones were reclaimed.
        self.assertEqual(retained, [1, 1, 1])

        with self._logged() as logs:
            result = self.verify()
            self.assertEqual(self.workspaces(), [])
            self.fetch()  # a download reads it again
        self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))
        self.assertEqual(self.workspaces(), [])
        self.assertTrue(any("reclaimed 1" in line for line in logs), logs)
        self.assertFalse(any(str(self.state_dir) in line for line in logs), logs)

    def test_a_workspace_left_by_an_interrupted_run_is_reclaimed(self) -> None:
        self.abandoned_workspace()
        with self._logged() as logs:
            self.fetch()
        self.assertEqual(self.workspaces(), [])
        self.assertTrue(any(line.startswith("WARNING:") and "reclaimed 1" in line for line in logs), logs)

    # -- only inactive, owned, recognisable work is reclaimed ----------------------

    def test_a_workspace_held_by_a_live_fetch_is_not_reclaimed(self) -> None:
        with self.scheduler.materialize(self.remote.artifact_id) as (_record, held):
            held_workspace = held.parent.name
            result = self.verify()  # its own fetch scans the folder while this one is open
            self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))
            self.assertEqual(self.workspaces(), [held_workspace])
            self.assertEqual(hashlib.sha256(held.read_bytes()).hexdigest(), self.remote.sha256)
            # A third fetch, entered while the first is still open, also leaves it.
            self.fetch()
            self.assertEqual(self.workspaces(), [held_workspace])
        self.assertEqual(self.workspaces(), [])

    def test_unrecognised_entries_are_left_in_place_and_reported(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "archive").write_bytes(b"not ours to remove")
        (self.state_dir / "backup-fetch-link").symlink_to(elsewhere, target_is_directory=True)
        self.unrecognised_workspace("backup-fetch-extra")
        nested = self.state_dir / "backup-fetch-nested"
        (nested / "archive").mkdir(parents=True)
        with self._logged() as logs:
            self.assertEqual(hashlib.sha256(self.fetch()).hexdigest(), self.remote.sha256)
        self.assertEqual(self.workspaces(), ["backup-fetch-extra", "backup-fetch-link", "backup-fetch-nested"])
        self.assertEqual((elsewhere / "archive").read_bytes(), b"not ours to remove")
        self.assertTrue((self.state_dir / "backup-fetch-extra" / "notes.txt").exists())
        self.assertTrue((nested / "archive").is_dir())
        self.assertTrue(
            any("left 3" in line and "not-a-directory=1" in line and "unrecognised-layout=2" in line for line in logs),
            logs,
        )
        self.assertFalse(any(str(self.state_dir) in line for line in logs), logs)

    def test_a_workspace_owned_by_another_account_is_left_in_place(self) -> None:
        workspace = self.abandoned_workspace()
        other = os.geteuid() + 1
        result = service._reclaim_fetch_workspaces(self.state_dir, owner=other)
        self.assertEqual((result.reclaimed, result.in_use), (0, 0))
        self.assertEqual(result.left, {"other-owner": 1})
        self.assertEqual((workspace / "archive").read_bytes(), b"full copy")
        # Control: the same directory is reclaimed for its own account.
        result = service._reclaim_fetch_workspaces(self.state_dir, owner=os.geteuid())
        self.assertEqual((result.reclaimed, result.left), (1, {}))
        self.assertFalse(workspace.exists())

    def test_a_plain_file_with_the_prefix_is_left_in_place(self) -> None:
        stray = self.state_dir / "backup-fetch-file"
        stray.write_bytes(b"not a workspace")
        result = service._reclaim_fetch_workspaces(self.state_dir, owner=os.geteuid())
        self.assertEqual(result.left, {"not-a-directory": 1})
        self.assertEqual(stray.read_bytes(), b"not a workspace")

    @NEEDS_PERMISSIONS
    def test_a_workspace_that_still_cannot_be_removed_is_reported(self) -> None:
        workspace = self.abandoned_workspace()
        workspace.chmod(0o500)
        self.locked.append(workspace)
        with self._logged() as logs:
            self.fetch()
        self.assertEqual(self.workspaces(), ["backup-fetch-abandoned"])
        self.assertEqual((workspace / "archive").read_bytes(), b"full copy")
        self.assertTrue(any("left 1" in line and "removal-failed=1" in line for line in logs), logs)
        self._unlock()
        self.fetch()
        self.assertEqual(self.workspaces(), [])

    # -- the bound -------------------------------------------------------------------

    def test_fetches_are_refused_once_the_abandoned_bound_is_reached(self) -> None:
        from history_service.backup_scheduler.api import build_app

        for index in range(ABANDONED_LIMIT):
            self.unrecognised_workspace(f"backup-fetch-kept-{index}")
        before = self.workspaces()
        verified_before = self.scheduler.get(self.remote.artifact_id).verified

        result = self.verify()
        self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
        self.assertIn(f"{ABANDONED_LIMIT} abandoned", result["detail"])
        self.assertIn(f"limit {ABANDONED_LIMIT}", result["detail"])
        self.assertNotIn(str(self.state_dir), result["detail"])
        self.assertEqual(self.scheduler.get(self.remote.artifact_id).verified, verified_before)
        self.assertEqual(self.workspaces(), before, "a refused fetch made no new copy")

        path = f"/internal/backups/{self.remote.artifact_id}/download"
        status, body = asgi_call(build_app(self.scheduler), "GET", path)
        self.assertEqual(status, 502, body)
        self.assertIn(f"limit {ABANDONED_LIMIT}", json.loads(body)["detail"])
        self.assertEqual(self.workspaces(), before)

        with self.assertRaises(service.FetchWorkspaceLimitError), self.scheduler.materialize(self.remote.artifact_id):
            self.fail("a refused fetch yielded a copy")

    def test_one_below_the_bound_still_fetches(self) -> None:
        for index in range(ABANDONED_LIMIT - 1):
            self.unrecognised_workspace(f"backup-fetch-kept-{index}")
        result = self.verify()
        self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))
        self.assertEqual(len(self.workspaces()), ABANDONED_LIMIT - 1)

    def test_live_fetches_do_not_count_towards_the_bound(self) -> None:
        for index in range(ABANDONED_LIMIT - 1):
            self.unrecognised_workspace(f"backup-fetch-kept-{index}")
        with self.scheduler.materialize(self.remote.artifact_id), self.scheduler.materialize(self.remote.artifact_id):
            result = self.verify()
        self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))

    def test_reclaiming_makes_room_under_the_bound(self) -> None:
        for index in range(ABANDONED_LIMIT):
            self.abandoned_workspace(f"backup-fetch-old-{index}")
        result = self.verify()
        self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))
        self.assertEqual(self.workspaces(), [])

    def test_the_bound_constant_matches(self) -> None:
        self.assertEqual(service.MAX_ABANDONED_FETCH_WORKSPACES, ABANDONED_LIMIT)


if __name__ == "__main__":
    unittest.main()
