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

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import select
import shutil
import subprocess
import sys
import threading
import time
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


class _FetchWorkspaceBase(SchedulerTestBase):
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
        with self.scheduler.materialize(self.remote.artifact_id) as (_record, handle):
            return handle.read()

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



class FetchWorkspaceReclaimTests(_FetchWorkspaceBase):
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
            (held_workspace,) = self.workspaces()
            result = self.verify()  # its own fetch scans the folder while this one is open
            self.assertEqual((result["ok"], result["outcome"]), (True, "matched"))
            self.assertEqual(self.workspaces(), [held_workspace])
            self.assertEqual(hashlib.sha256(held.read()).hexdigest(), self.remote.sha256)
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


# Another process acting on the state folder. Commands arrive as JSON lines and
# each is answered with one line, so a test can stop the scheduler at an exact
# step, let the other process act, and continue.
_HELPER = r"""
import fcntl, json, os, sys
for line in sys.stdin:
    op, *args = json.loads(line)
    try:
        if op == "swap-ancestor":  # state -> parked; state becomes a link to elsewhere
            os.rename(args[0], args[1])
            os.symlink(args[2], args[0])
        elif op == "restore-ancestor":
            os.unlink(args[0])
            os.rename(args[1], args[0])
        elif op == "replace":  # without the lock: move the directory away and recreate the name
            os.rename(args[0], args[1])
            os.mkdir(args[0], 0o700)
        elif op == "replace-locked":  # as any lock holder would: lock, then move and recreate
            lock = os.open(args[0], os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                os.rename(args[1], args[2])
            except FileNotFoundError:
                pass
            os.mkdir(args[1], 0o700)
            os.close(lock)
        print("ok", flush=True)
    except OSError as exc:
        print("error", type(exc).__name__, flush=True)
"""

# A second scheduler process making a fetch workspace, paused right after the
# directory exists and before its creator has opened or locked it.
_CREATOR = r"""
import os, sys
from pathlib import Path
from types import SimpleNamespace
from history_service.backup_scheduler import service

real_mkdir = os.mkdir

def paused_mkdir(path, *args, **kwargs):
    real_mkdir(path, *args, **kwargs)
    name = os.path.basename(os.fspath(path))
    if name.startswith(service.FETCH_WORKSPACE_PREFIX):
        print("made", name, flush=True)
        sys.stdin.readline()

os.mkdir = paused_mkdir
scheduler = service.BackupScheduler.__new__(service.BackupScheduler)
scheduler.paths = SimpleNamespace(state_dir=Path(sys.argv[1]))
try:
    held = scheduler._new_fetch_workspace()
except Exception as exc:
    print("error", type(exc).__name__, flush=True)
else:
    print("held", flush=True)
sys.stdin.readline()
"""

REPO_ROOT = Path(__file__).resolve().parents[1]
# Names in the state folder, written out so a change is a visible test edit.
STATE_LOCK_NAME = ".backup-fetch.lock"
FETCH_MARKER_NAME = "fetch-workspace"


class _Process:
    """A real second process driven line by line over pipes."""

    def __init__(self, test: unittest.TestCase, script: str, *args: str) -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n{script}", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=REPO_ROOT,
        )
        self._buffer = b""
        test.addCleanup(self.close)

    def send(self, *command: str) -> None:
        self._process.stdin.write((json.dumps(list(command)) + "\n").encode())
        self._process.stdin.flush()

    def line(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        stream = self._process.stdout.fileno()
        while b"\n" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([stream], [], [], left)[0]:
                return None
            chunk = os.read(stream, 4096)
            if not chunk:
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode()

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.stdin.close()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process.stdout.close()


class PinnedStateFolderTests(_FetchWorkspaceBase):
    """Reclamation works on the state folder it checked, under a lock every process shares."""

    def _hook(self, name: str, victim: str, action):
        """Run ``action`` once, just before ``os.<name>`` touches the entry ``victim``."""

        real = getattr(os, name)
        fired: list[bool] = []

        def hooked(path, *args, **kwargs):
            if not fired and os.path.basename(os.fspath(path)) == victim:
                fired.append(True)
                return action(real, path, *args, **kwargs)
            return real(path, *args, **kwargs)

        return mock.patch.object(os, name, hooked)

    def _download_attempt(self, artifact_id: str, materialize, *, chunk_size: int):
        from history_service.backup_scheduler import api

        app = api.build_app(self.scheduler)
        route = next(route for route in app.routes if getattr(route, "path", None) == "/internal/backups/{artifact_id}/download")

        async def attempt():
            sent: list[dict] = []

            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            with (
                mock.patch.object(self.scheduler, "materialize", materialize),
                mock.patch.object(api, "_STREAM_CHUNK_BYTES", chunk_size),
            ):
                response = await route.endpoint(artifact_id)  # type: ignore[attr-defined]
                caught = None
                try:
                    await response(
                        {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}},
                        receive,
                        send,
                    )
                except Exception as exc:  # the response must abort after its headers
                    caught = exc
            body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
            return caught, body

        return asyncio.run(attempt())

    def test_a_state_folder_swapped_for_a_link_cannot_redirect_reclamation(self) -> None:
        victim = "backup-fetch-victim"
        self.abandoned_workspace(victim, content=b"abandoned copy")
        elsewhere = self.root / "elsewhere"
        (elsewhere / victim).mkdir(parents=True, mode=0o700)
        (elsewhere / victim / "archive").write_bytes(b"not part of the state folder")
        parked = self.root / "state-parked"
        helper = _Process(self, _HELPER)

        def swapped_while_opening(real, path, *args, **kwargs):
            # Another process swaps the state folder for a link just before the
            # workspace is opened, and puts it back right after.
            helper.send("swap-ancestor", str(self.state_dir), str(parked), str(elsewhere))
            self.assertEqual(helper.line(10), "ok")
            try:
                return real(path, *args, **kwargs)
            finally:
                helper.send("restore-ancestor", str(self.state_dir), str(parked))
                self.assertEqual(helper.line(10), "ok")

        with self._hook("open", victim, swapped_while_opening):
            self.assertEqual(hashlib.sha256(self.fetch()).hexdigest(), self.remote.sha256)
        outside = elsewhere / victim / "archive"
        self.assertTrue(outside.is_file(), "a file outside the state folder was deleted")
        self.assertEqual(outside.read_bytes(), b"not part of the state folder")
        self.assertFalse((self.state_dir / victim).exists(), "the checked workspace is the one reclaimed")
        self.assertEqual(self.workspaces(), [])

    def test_a_lock_holder_cannot_swap_a_workspace_between_check_and_removal(self) -> None:
        victim = "backup-fetch-victim"
        self.abandoned_workspace(victim)
        moved = self.root / "moved-away"
        helper = _Process(self, _HELPER)
        answered: list[str | None] = []

        real_rename = service._rename_noreplace_in

        def replaced_before_removal(root, source, target):
            # Another process that takes the state-folder lock tries to move the
            # checked workspace while its quarantine rename is still protected.
            if source == victim:
                helper.send(
                    "replace-locked",
                    str(self.state_dir / STATE_LOCK_NAME),
                    str(self.state_dir / victim),
                    str(moved),
                )
                answered.append(helper.line(1.0))  # it must still be waiting for the lock
            return real_rename(root, source, target)

        with mock.patch.object(service, "_rename_noreplace_in", replaced_before_removal):
            self.fetch()
        self.assertEqual(answered, [None], "the removal was not serialised with other lock holders")
        self.assertEqual(helper.line(10), "ok")
        self.assertTrue((self.state_dir / victim).is_dir(), "a directory made after the check was removed")
        self.assertFalse(moved.exists(), "the checked workspace itself was reclaimed")

    def test_a_new_workspace_of_another_process_is_not_reclaimed_before_it_is_locked(self) -> None:
        creator = _Process(self, _CREATOR, str(self.state_dir))
        made = creator.line(60)
        self.assertIsNotNone(made, "the second process never made its workspace")
        name = str(made).split()[1]
        fetched: list[bytes] = []
        thread = threading.Thread(target=lambda: fetched.append(self.fetch()))
        thread.start()
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and (self.state_dir / name).exists() and thread.is_alive():
                time.sleep(0.02)
            creator.send("go")
            self.assertEqual(creator.line(30), "held", "a live fetch lost its new workspace")
        finally:
            thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual([hashlib.sha256(data).hexdigest() for data in fetched], [self.remote.sha256])
        self.assertEqual(self.workspaces(), [name], "the live workspace survives the other fetch")
        # Once that process is gone, its marked workspace is abandoned and reclaimed.
        creator.close()
        self.fetch()
        self.assertEqual(self.workspaces(), [])

    def test_a_workspace_swapped_before_its_identity_check_is_left(self) -> None:
        victim = "backup-fetch-victim"
        self.abandoned_workspace(victim)
        moved = self.root / "moved-away"
        helper = _Process(self, _HELPER)

        def replaced_after_emptying(real, path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            helper.send("replace", str(self.state_dir / victim), str(moved))
            self.assertEqual(helper.line(10), "ok")
            return result

        with self._hook("unlink", "archive", replaced_after_emptying), self._logged() as logs:
            self.fetch()
        self.assertTrue((self.state_dir / victim).is_dir(), "a directory put under the name was removed")
        self.assertTrue(moved.is_dir())
        self.assertTrue(any("unrecognised-layout=1" in line for line in logs), logs)

    def test_a_workspace_swapped_after_its_identity_check_is_not_removed(self) -> None:
        victim = "backup-fetch-victim"
        self.abandoned_workspace(victim)
        moved = self.root / "moved-away"
        replacement_identity: list[tuple[int, int]] = []

        def replaced_after_identity(real, path, *args, **kwargs):
            checked = real(path, *args, **kwargs)
            os.rename(self.state_dir / victim, moved)
            replacement = self.state_dir / victim
            replacement.mkdir(mode=0o700)
            metadata = replacement.lstat()
            replacement_identity.append((metadata.st_dev, metadata.st_ino))
            return checked

        with self._hook("stat", victim, replaced_after_identity), self._logged() as logs:
            self.fetch()
        self.assertTrue(moved.is_dir(), "the checked workspace was not parked by the hook")
        remaining = [
            entry.lstat()
            for entry in self.state_dir.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        ]
        self.assertTrue(
            any((entry.st_dev, entry.st_ino) in replacement_identity for entry in remaining),
            "the replacement directory was removed by name",
        )
        self.assertTrue(any("unrecognised-layout=1" in line for line in logs), logs)

    def test_fetch_destination_stays_under_the_pinned_workspace(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        parked = self.root / "state-parked"

        def swapped_while_creating_destination(real, path, *args, **kwargs):
            workspace = next(
                entry
                for entry in self.state_dir.iterdir()
                if entry.name.startswith(service.FETCH_WORKSPACE_PREFIX)
            )
            outside_workspace = elsewhere / workspace.name
            outside_workspace.mkdir(mode=0o700)
            self.state_dir.rename(parked)
            self.state_dir.symlink_to(elsewhere, target_is_directory=True)
            try:
                return real(path, *args, **kwargs)
            finally:
                self.state_dir.unlink()
                parked.rename(self.state_dir)

        data: bytes | None = None
        try:
            with self._hook("open", "archive", swapped_while_creating_destination):
                data = self.fetch()
        except FileNotFoundError:
            pass
        self.assertEqual(list(elsewhere.rglob("archive")), [], "the fetch followed the swapped ancestor")
        if data is None:
            self.fail("the pinned workspace did not receive the fetched bytes")
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.remote.sha256)

    def test_download_streams_the_file_that_passed_verification(self) -> None:
        from history_service.backup_scheduler.api import build_app

        materialize = self.scheduler.materialize

        @contextlib.contextmanager
        def replace_name_after_materialize(artifact_id: str):
            with materialize(artifact_id) as opened:
                workspace = next(
                    entry
                    for entry in self.state_dir.iterdir()
                    if entry.name.startswith(service.FETCH_WORKSPACE_PREFIX)
                )
                replacement = workspace / "replacement"
                replacement.write_bytes(b"x" * self.remote.size)
                os.replace(replacement, workspace / "archive")
                yield opened

        with mock.patch.object(self.scheduler, "materialize", replace_name_after_materialize):
            status, body = asgi_call(
                build_app(self.scheduler),
                "GET",
                f"/internal/backups/{self.remote.artifact_id}/download",
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.remote.sha256)

    def test_local_download_streams_the_file_that_passed_verification(self) -> None:
        from history_service.backup_scheduler.api import build_app

        local = self.scheduler.catalog.list(location=service.LOCAL_LOCATION)[0]
        materialize = self.scheduler.materialize

        @contextlib.contextmanager
        def replace_name_after_materialize(artifact_id: str):
            with materialize(artifact_id) as opened:
                path = self.scheduler._local_path(local)
                replacement = path.with_name(f"{path.name}.replacement")
                replacement.write_bytes(b"x" * local.size)
                os.replace(replacement, path)
                yield opened

        with mock.patch.object(self.scheduler, "materialize", replace_name_after_materialize):
            status, body = asgi_call(
                build_app(self.scheduler),
                "GET",
                f"/internal/backups/{local.artifact_id}/download",
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(hashlib.sha256(body).hexdigest(), local.sha256)

    def test_download_stops_short_if_the_open_file_changes(self) -> None:
        from history_service.backup_scheduler.api import build_app

        materialize = self.scheduler.materialize

        @contextlib.contextmanager
        def overwrite_after_materialize(artifact_id: str):
            with materialize(artifact_id) as opened:
                workspace = next(
                    entry
                    for entry in self.state_dir.iterdir()
                    if entry.name.startswith(service.FETCH_WORKSPACE_PREFIX)
                )
                with (workspace / "archive").open("r+b") as replacement:
                    replacement.write(b"x" * self.remote.size)
                yield opened

        with mock.patch.object(self.scheduler, "materialize", overwrite_after_materialize):
            with self.assertRaises(service.ArchiveIntegrityError):
                asgi_call(
                    build_app(self.scheduler),
                    "GET",
                    f"/internal/backups/{self.remote.artifact_id}/download",
                )
        self.assertEqual(self.workspaces(), [])

    def test_download_holds_all_expected_bytes_until_an_excess_byte_is_refused(self) -> None:
        materialize = self.scheduler.materialize

        @contextlib.contextmanager
        def overwrite_after_materialize(artifact_id: str):
            with materialize(artifact_id) as opened:
                workspace = next(self.state_dir.glob(f"{service.FETCH_WORKSPACE_PREFIX}*"))
                with (workspace / "archive").open("r+b") as replacement:
                    replacement.write(b"x" * self.remote.size + b"y")
                    replacement.truncate()
                yield opened

        caught, body = self._download_attempt(
            self.remote.artifact_id,
            overwrite_after_materialize,
            chunk_size=self.remote.size,
        )
        self.assertIsInstance(caught, service.ArchiveIntegrityError)
        self.assertEqual(body, b"", "the final expected bytes were released before EOF")
        self.assertEqual(self.workspaces(), [])

    def test_download_holds_a_short_file_until_its_size_is_refused(self) -> None:
        materialize = self.scheduler.materialize

        @contextlib.contextmanager
        def truncate_after_materialize(artifact_id: str):
            with materialize(artifact_id) as opened:
                workspace = next(self.state_dir.glob(f"{service.FETCH_WORKSPACE_PREFIX}*"))
                with (workspace / "archive").open("r+b") as replacement:
                    replacement.truncate(self.remote.size - 1)
                yield opened

        caught, body = self._download_attempt(
            self.remote.artifact_id,
            truncate_after_materialize,
            chunk_size=self.remote.size,
        )
        self.assertIsInstance(caught, service.ArchiveIntegrityError)
        self.assertEqual(body, b"", "a short file released its final bytes")
        self.assertEqual(self.workspaces(), [])

    def test_an_empty_workspace_without_its_fetch_marker_is_left_in_place(self) -> None:
        empty = self.state_dir / "backup-fetch-empty"
        empty.mkdir(mode=0o700)
        with self._logged() as logs:
            self.fetch()
        self.assertTrue(empty.is_dir(), "an empty directory proves nothing about who made it")
        self.assertTrue(any("left 1" in line and "unrecognised-layout=1" in line for line in logs), logs)

    def test_a_workspace_its_fetch_marked_is_reclaimed_before_any_bytes_arrive(self) -> None:
        marked = self.state_dir / "backup-fetch-marked"
        marked.mkdir(mode=0o700)
        (marked / FETCH_MARKER_NAME).write_bytes(b"")
        self.fetch()
        self.assertEqual(self.workspaces(), [])

    def test_a_state_folder_that_is_a_link_is_refused(self) -> None:
        real = self.root / "state-real"
        self.state_dir.rename(real)
        self.state_dir.symlink_to(real, target_is_directory=True)
        self.abandoned_workspace_at(real)
        result = self.verify()
        self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
        self.assertIn("state folder", result["detail"])
        self.assertTrue((real / "backup-fetch-abandoned" / "archive").exists())
        self.assertEqual(sorted(p.name for p in real.glob("backup-fetch-*")), ["backup-fetch-abandoned"])

    def test_a_state_folder_others_can_write_is_refused(self) -> None:
        self.abandoned_workspace()
        self.state_dir.chmod(0o770)
        self.addCleanup(self.state_dir.chmod, 0o700)
        result = self.verify()
        self.assertEqual((result["ok"], result["outcome"]), (False, "not_read"))
        self.assertIn("state folder", result["detail"])
        self.assertEqual(self.workspaces(), ["backup-fetch-abandoned"])

    def abandoned_workspace_at(self, folder: Path) -> None:
        (folder / "backup-fetch-abandoned").mkdir(mode=0o700)
        (folder / "backup-fetch-abandoned" / "archive").write_bytes(b"full copy")

    def test_stale_keeper_scratch_removal_stays_in_the_state_folder(self) -> None:
        victim = "keeper-check-stale"
        (self.state_dir / victim / "nested").mkdir(parents=True, mode=0o700)
        elsewhere = self.root / "elsewhere"
        (elsewhere / victim / "nested").mkdir(parents=True)
        (elsewhere / victim / "nested" / "data").write_bytes(b"not part of the state folder")
        parked = self.root / "state-parked"
        helper = _Process(self, _HELPER)
        real_rmtree = shutil.rmtree

        def swapped_while_removing(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) != victim:
                return real_rmtree(path, *args, **kwargs)
            helper.send("swap-ancestor", str(self.state_dir), str(parked), str(elsewhere))
            self.assertEqual(helper.line(10), "ok")
            try:
                return real_rmtree(path, *args, **kwargs)
            finally:
                helper.send("restore-ancestor", str(self.state_dir), str(parked))
                self.assertEqual(helper.line(10), "ok")

        with mock.patch.object(service.shutil, "rmtree", swapped_while_removing):
            service._remove_stale_keeper_scratch(self.state_dir)
        outside = elsewhere / victim / "nested" / "data"
        self.assertTrue(outside.is_file(), "a tree outside the state folder was deleted")
        self.assertEqual(outside.read_bytes(), b"not part of the state folder")
        self.assertFalse((self.state_dir / victim).exists())


if __name__ == "__main__":
    unittest.main()
