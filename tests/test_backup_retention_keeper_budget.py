"""Retention keeper checks stay bounded and fail closed (#1188).

Before a retention deletion, grooming reads back the newest kept copy and stops
as soon as one kept copy matches. A missing or mismatching keeper is downgraded
and the next one is tried, but only a small fixed number of reads per class and
location; running out of reads refuses the deletions. Synthetic files only: the
remote target is the real filesystem transport over a temporary directory.
"""

from __future__ import annotations

import hashlib
import http.client
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
from tests.test_backup_integration import TARGET, SchedulerTestBase, asgi_call
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

    def test_apply_response_reports_a_safety_replan(self) -> None:
        from history_service.backup_scheduler.api import build_app

        scheduler = self.make({"full": {"enabled": True, "local_keep": 2}})
        older = scheduler.run_now("full")
        self.now += timedelta(hours=1)
        newest = scheduler.run_now("full")
        (self._paths.local_dir / newest.name).unlink()
        scheduler.close()
        scheduler = self.make({"full": {"enabled": True, "local_keep": 1}})
        app = build_app(scheduler)
        plan = json.loads(asgi_call(app, "GET", "/internal/backups/lifecycle/plan")[1])

        status, body = asgi_call(app, "POST", "/internal/backups/lifecycle/apply", {"plan_token": plan["plan_token"]})

        self.assertEqual(status, 200)
        response = json.loads(body)
        # The safe replan is still a complete run, but it says what it found.
        self.assertTrue(response["ok"])
        self.assertIn("downgraded", response, "the safety replan is not reported")
        self.assertEqual(response["deleted"], [])
        self.assertEqual(response["downgraded"], [newest.artifact_id])
        self.assertEqual(response["kept_after_replan"], [older.artifact_id])
        self.assertTrue((self._paths.local_dir / older.name).exists())

    def test_one_full_run_reads_at_most_fourteen_full_copies(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 100}})
        for _ in range(17):
            scheduler.run_now("full")
            self.now += timedelta(days=1)
        prior_newest = scheduler.catalog.list(backup_class="full", location="local")[-1]
        (self._paths.local_dir / prior_newest.name).write_bytes(b"X" * prior_newest.size)
        scheduler.close()
        scheduler = self.make({"full": {"enabled": True, "local_keep": 14}})
        real_ship = scheduler._ship
        reads: list[str] = []

        def ship_then_corrupt(record):
            failures = real_ship(record)
            (self._paths.local_dir / record.name).write_bytes(b"X" * record.size)
            return failures

        def counting(record, target):
            reads.append(record.artifact_id)
            return local_payload_digest(record, target)

        with patch.object(scheduler, "_ship", side_effect=ship_then_corrupt), patch.object(
            scheduler_service, "local_payload_digest", side_effect=counting
        ):
            new = scheduler.run_now("full")

        # Keeper check and sidecar credit together stay within one 14-read
        # allowance; a copy read by the keeper check is not read again.
        self.assertLessEqual(len(reads), scheduler_service.HISTORY_REPLACEMENT_COPIES)
        self.assertEqual(len(reads), len(set(reads)))
        self.assertEqual(reads[:2], [new.artifact_id, prior_newest.artifact_id])
        self.assertFalse(scheduler.catalog.get(new.artifact_id).verified)
        self.assertFalse(scheduler.catalog.get(prior_newest.artifact_id).verified)
        self.assertTrue((self._paths.local_dir / new.name).exists())
        self.assertTrue((self._paths.local_dir / prior_newest.name).exists())

    def test_grooming_detail_counts_downgraded_copies_in_plain_words(self) -> None:
        from types import SimpleNamespace

        scheduler = self.make({"full": {"enabled": True}})
        for count, expected in (
            # "N of the copies it kept", so the count cannot read as a tally of kept copies.
            (1, "1 of the copies it kept failed the size and SHA-256 check and was marked unverified."),
            (3, "3 of the copies it kept failed the size and SHA-256 check and were marked unverified."),
        ):
            with self.subTest(count=count):
                result = SimpleNamespace(
                    failed_locations={}, failed=None, error=None, deleted=(), already_missing=(),
                    downgraded=tuple(object() for _ in range(count)),
                )
                scheduler._record_grooming_result(result)
                grooming = json.loads(self._paths.status_file.read_text())["grooming"]
                self.assertTrue(grooming["ok"])
                self.assertEqual(grooming["detail"], expected)
        source = (Path(__file__).resolve().parents[1] / "history_service/backup_scheduler/service.py").read_text()
        self.assertFalse("kept backup copy/copies" in source or "kept copy/copies" in source, "copy/copies shortcut")

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


class _LateScheduler:
    """A Unix socket that reads one request and answers only when released."""

    def __init__(self, path: Path) -> None:
        import socket

        self.path = path
        self.release = threading.Event()
        self.requests: list[bytes] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self.server.accept()
        except OSError:
            return
        with connection:
            self.requests.append(connection.recv(65536))
            self.release.wait(HANG_TIMEOUT_SECONDS)
            body = b'{"ok": true, "deleted": [], "already_missing": [], "failed": null, "not_attempted": []}'
            try:
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
            except OSError:
                pass

    def close(self) -> None:
        self.release.set()
        self.server.close()
        self.thread.join(HANG_TIMEOUT_SECONDS)


# What the admin apply route says when the clean-up's own answer did not arrive.
# Each is chosen from one follow-up status read of the scheduler, never from
# the timeout alone.
CLEANUP_RUNNING = (
    "A clean-up is running on the backup service and can still delete the copies in its plan. "
    "The result of this request did not reach the admin service. This page checks again by itself; "
    "when the clean-up finishes, the Last clean-up line shows the latest result the backup service recorded."
)
CLEANUP_NOT_RUNNING = (
    "The result of this clean-up did not reach the admin service, and the backup service reports that "
    "no clean-up is running now. The Last clean-up line shows the latest result the backup service recorded."
)
CLEANUP_STATE_UNKNOWN = (
    "The result of this clean-up did not reach the admin service, and the backup service did not say "
    "whether a clean-up is still running, so its state is not known. It may still be running and may "
    "still delete the copies in its plan. This page checks again by itself; the Last clean-up line shows "
    "the latest result the backup service recorded."
)


class AdminCleanupApplyWaitTests(unittest.TestCase):
    """A manual clean-up apply gets its own wait; a missing answer is never read as a state."""

    def setUp(self) -> None:
        import tempfile

        from admin_service import main as admin_main
        from admin_service import route_support
        from admin_service import routes as admin_routes
        from admin_service.services import backup_scheduler_client as client_module

        self.admin_main = admin_main
        self.admin_routes = admin_routes
        self.client_module = client_module
        route_support.get_backup_scheduler_client.cache_clear()
        self.addCleanup(route_support.get_backup_scheduler_client.cache_clear)
        # Short directory: Unix socket paths have a small length limit.
        self.temp = tempfile.TemporaryDirectory(prefix="ka-")
        self.addCleanup(self.temp.cleanup)
        self.socket_path = Path(self.temp.name) / "s.sock"

    def call(self, method: str, path: str, body=None):
        with patch.object(self.admin_main, "_request_origin_allowed", return_value=True), \
                patch.object(self.admin_main, "_basic_auth_matches", return_value=True):
            return asgi_call(self.admin_main.app, method, path, body, {"origin": "http://test"})

    def test_apply_wait_is_longer_than_ordinary_calls_and_shorter_than_the_browser(self) -> None:
        apply_timeout = getattr(self.client_module, "LIFECYCLE_APPLY_TIMEOUT_SECONDS", None)
        if not isinstance(apply_timeout, float):
            self.fail("clean-up apply has no wait of its own")
        browser_ms = 15 * 60 * 1000  # LONG_TIMEOUT_MS in admin_backups.js
        source = (Path(__file__).resolve().parents[1] / "admin_service/static/admin_backups.js").read_text()
        self.assertIn("const LONG_TIMEOUT_MS = 15 * 60 * 1000;", source)
        self.assertGreater(apply_timeout, self.client_module.DEFAULT_TIMEOUT_SECONDS)
        self.assertLess(apply_timeout * 1000, browser_ms)

    def test_late_answer_with_no_status_read_is_reported_as_not_known(self) -> None:
        # This fake answers one connection only, so the follow-up status read
        # gets no answer either: nothing was observed, so nothing is claimed.
        late = _LateScheduler(self.socket_path)
        self.addCleanup(late.close)
        client = self.client_module.BackupSchedulerClient(str(self.socket_path))
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), patch.object(
            self.client_module, "LIFECYCLE_APPLY_TIMEOUT_SECONDS", 0.5, create=True
        ), patch.object(self.client_module, "DEFAULT_TIMEOUT_SECONDS", 0.5), patch.object(client, "timeout", 0.5):
            status, body = self.call("POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"})

        self.assertTrue(late.requests and late.requests[0].startswith(b"POST /internal/backups/lifecycle/apply"))
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload, {"state": "cleanup_state_unknown", "detail": CLEANUP_STATE_UNKNOWN})

    def test_scheduler_answer_inside_the_wait_is_forwarded(self) -> None:
        late = _LateScheduler(self.socket_path)
        self.addCleanup(late.close)
        client = self.client_module.BackupSchedulerClient(str(self.socket_path))
        threading.Timer(0.3, late.release.set).start()
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), patch.object(
            client, "timeout", 0.1
        ):
            status, body = self.call("POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"})

        # The ordinary 0.1 s wait would have given up; the apply wait does not.
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["deleted"], [])

    def test_missing_scheduler_socket_still_reports_unavailable(self) -> None:
        client = self.client_module.BackupSchedulerClient(str(self.socket_path))
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client):
            status, body = self.call("POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"})

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["detail"], "The backup scheduler service is not available.")

    def test_page_uses_the_same_words_as_the_route_for_each_state(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "admin_service/static/admin_backups.js").read_text()
        import re

        # Join the page's "..." + "..." string pieces back into one string.
        joined = re.sub(r'"\s*\+\s*"', "", " ".join(source.split()))
        for text in (CLEANUP_RUNNING, CLEANUP_NOT_RUNNING, CLEANUP_STATE_UNKNOWN):
            with self.subTest(text=text[:40]):
                self.assertTrue(f'"{text}"' in joined, text)


class _SchedulerBridge:
    """Serves the real scheduler app over a Unix socket, one thread per connection.

    It stands in for the scheduler's HTTP server so a test can hold one part of
    one answer: the whole status read, or the body of an apply answer after the
    scheduler has really finished (and released its job reservation).
    """

    def __init__(self, path: Path, app) -> None:
        import socket

        self.app = app
        self.release = threading.Event()
        self.hold_apply_body = False
        self.hold_status_read = False
        self.seen: list[tuple[str, str]] = []
        self.apply_finished = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.server.settimeout(0.05)  # so close() can stop the accept loop
        self.closed = threading.Event()
        self.workers: list[threading.Thread] = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while not self.closed.is_set():
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            connection.settimeout(None)
            worker = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def _serve(self, connection) -> None:
        with connection:
            connection.settimeout(HANG_TIMEOUT_SECONDS)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                method, path, _version = lines[0].split(" ", 2)
                length = 0
                for line in lines[1:]:
                    name, _, value = line.partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                while len(rest) < length:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    rest += chunk
            except OSError:
                return
            self.seen.append((method, path))
            if method == "GET" and path == "/internal/backups" and self.hold_status_read:
                self.release.wait(HANG_TIMEOUT_SECONDS)
                return
            status, content = asgi_call(self.app, method, path, json.loads(rest) if rest else None)
            is_apply = path == "/internal/backups/lifecycle/apply"
            if is_apply:
                self.apply_finished.set()
            response = (
                f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(content)}\r\nConnection: close\r\n\r\n"
            ).encode()
            try:
                if is_apply and self.hold_apply_body:
                    connection.sendall(response + content[:1])
                    self.release.wait(HANG_TIMEOUT_SECONDS)
                    return
                connection.sendall(response + content)
            except OSError:
                pass

    def close(self) -> None:
        self.release.set()
        self.closed.set()
        self.thread.join(HANG_TIMEOUT_SECONDS)
        self.server.close()
        for worker in self.workers:
            worker.join(HANG_TIMEOUT_SECONDS)


class AdminCleanupNoAnswerTests(SchedulerTestBase):
    """When the clean-up's answer does not arrive, the route says only what one status read shows.

    The real admin route, the real Unix-socket client and the real scheduler app
    and scheduler; only the socket server in between can hold part of an answer.
    """

    APPLY_WAIT = 0.4
    STATUS_WAIT = 2.0

    def setUp(self) -> None:
        import tempfile

        super().setUp()
        from admin_service import main as admin_main
        from admin_service import route_support
        from admin_service import routes as admin_routes
        from admin_service.services import backup_scheduler_client as client_module

        self.admin_main = admin_main
        self.admin_routes = admin_routes
        self.client_module = client_module
        route_support.get_backup_scheduler_client.cache_clear()
        self.addCleanup(route_support.get_backup_scheduler_client.cache_clear)
        sockets = tempfile.TemporaryDirectory(prefix="kb-")
        self.addCleanup(sockets.cleanup)
        self.socket_path = Path(sockets.name) / "s.sock"

    def two_local_fulls(self):
        """Two local FULLs and a scheduler that keeps one: the plan deletes the older."""

        scheduler = self.make({"full": {"enabled": True, "local_keep": 2}})
        older = scheduler.run_now("full")
        self.now += timedelta(hours=1)
        newest = scheduler.run_now("full")
        scheduler.close()
        return older, newest, self.make({"full": {"enabled": True, "local_keep": 1}})

    def bridge(self, scheduler) -> _SchedulerBridge:
        from history_service.backup_scheduler.api import build_app

        bridge = _SchedulerBridge(self.socket_path, build_app(scheduler))
        self.addCleanup(bridge.close)
        return bridge

    def plan_token(self, bridge: _SchedulerBridge) -> str:
        return json.loads(asgi_call(bridge.app, "GET", "/internal/backups/lifecycle/plan")[1])["plan_token"]

    def apply(self, token: str):
        client = self.client_module.BackupSchedulerClient(str(self.socket_path), timeout=self.STATUS_WAIT)
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), patch.object(
            self.client_module, "LIFECYCLE_APPLY_TIMEOUT_SECONDS", self.APPLY_WAIT
        ), patch.object(self.admin_main, "_request_origin_allowed", return_value=True), patch.object(
            self.admin_main, "_basic_auth_matches", return_value=True
        ):
            status, body = asgi_call(
                self.admin_main.app, "POST", "/api/admin/backups/lifecycle/apply", {"plan_token": token},
                {"origin": "http://test"},
            )
        return status, json.loads(body)

    def hold_lifecycle_apply(self):
        """Hold the real apply inside the scheduler's job reservation until released."""

        gate = threading.Event()
        entered = threading.Event()
        real_apply = LifecycleManager.apply

        def held(manager, *args, **kwargs):
            entered.set()
            gate.wait(HANG_TIMEOUT_SECONDS)
            return real_apply(manager, *args, **kwargs)

        holder = patch.object(LifecycleManager, "apply", held)
        holder.start()
        self.addCleanup(holder.stop)
        self.addCleanup(gate.set)
        return gate, entered

    def test_answer_body_lost_after_a_finished_success_is_not_reported_as_running(self) -> None:
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        bridge.hold_apply_body = True

        status, payload = self.apply(self.plan_token(bridge))

        self.assertTrue(bridge.apply_finished.is_set())
        self.assertIsNone(scheduler.running)
        grooming = scheduler.library()["grooming"]
        self.assertTrue(grooming["ok"])
        self.assertEqual(grooming["deleted"], 1)
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, {"state": "cleanup_not_running", "detail": CLEANUP_NOT_RUNNING})
        # One bounded follow-up question, after the apply.
        self.assertEqual(bridge.seen[-1], ("GET", "/internal/backups"))
        self.assertEqual([path for _method, path in bridge.seen].count("/internal/backups"), 1)

    def test_answer_body_lost_after_a_finished_failure_is_not_reported_as_running(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 2, "remote_keep": 2}, "targets": [TARGET]})
        scheduler.run_now("full")
        self.now += timedelta(hours=1)
        scheduler.run_now("full")
        scheduler.close()
        scheduler = self.make({"full": {"enabled": True, "local_keep": 2, "remote_keep": 1}, "targets": [TARGET]})
        bridge = self.bridge(scheduler)
        bridge.hold_apply_body = True
        token = self.plan_token(bridge)

        with patch.object(scheduler_service, "_scratch_free_bytes", return_value=0):
            status, payload = self.apply(token)

        self.assertTrue(bridge.apply_finished.is_set())
        self.assertIsNone(scheduler.running)
        grooming = scheduler.library()["grooming"]
        self.assertFalse(grooming["ok"])
        self.assertIn("keeper-scratch-capacity", grooming["failed_locations"]["nas"])
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, {"state": "cleanup_not_running", "detail": CLEANUP_NOT_RUNNING})

    def test_answer_headers_late_while_the_scheduler_runs_the_clean_up(self) -> None:
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        gate, entered = self.hold_lifecycle_apply()

        status, payload = self.apply(token)

        self.assertTrue(entered.is_set())
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, {"state": "cleanup_running", "detail": CLEANUP_RUNNING})
        self.assertEqual(scheduler.running["backup_class"], "lifecycle")
        gate.set()
        self.assertTrue(bridge.apply_finished.wait(HANG_TIMEOUT_SECONDS))
        self.assertFalse((self._paths.local_dir / older.name).exists())

    def test_answer_headers_late_and_no_status_read_is_not_known(self) -> None:
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        bridge.hold_status_read = True
        token = self.plan_token(bridge)
        gate, _entered = self.hold_lifecycle_apply()

        with patch.object(self, "STATUS_WAIT", 0.3):
            status, payload = self.apply(token)

        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, {"state": "cleanup_state_unknown", "detail": CLEANUP_STATE_UNKNOWN})
        self.assertEqual(bridge.seen[-1], ("GET", "/internal/backups"))
        gate.set()

    def test_status_reads_that_do_not_show_the_state_are_not_known(self) -> None:
        from admin_service.services.backup_scheduler_client import SchedulerResponse

        cases = {
            "error status": SchedulerResponse(status=500, payload={"detail": "boom"}),
            "not an object": SchedulerResponse(status=200, payload=["running"]),
            "unavailable library": SchedulerResponse(status=200, payload={"available": False, "running": None}),
            "no running field": SchedulerResponse(status=200, payload={"available": True}),
            "running not an object": SchedulerResponse(status=200, payload={"available": True, "running": "yes"}),
            "running without a class": SchedulerResponse(status=200, payload={"available": True, "running": {}}),
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify_after_lost_answer(answer)[0], "cleanup_state_unknown")
        for name, running in {"nothing": None, "a backup": {"backup_class": "full", "started_at": "x"}}.items():
            with self.subTest(name):
                answer = SchedulerResponse(status=200, payload={"available": True, "running": running})
                self.assertEqual(self.classify_after_lost_answer(answer)[0], "cleanup_not_running")
        answer = SchedulerResponse(status=200, payload={"available": True, "running": {"backup_class": "lifecycle"}})
        self.assertEqual(self.classify_after_lost_answer(answer)[0], "cleanup_running")

    def classify_after_lost_answer(self, follow_up):
        """Drive the route with a client whose apply answer is lost and whose status read returns ``follow_up``."""

        module = self.client_module

        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method, path, **_kwargs):
                self.calls.append((method, path))
                if path == "/internal/backups/lifecycle/apply":
                    raise module.SchedulerTimeoutError("no answer")
                return follow_up

        client = Client()
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), patch.object(
            self.admin_main, "_request_origin_allowed", return_value=True
        ), patch.object(self.admin_main, "_basic_auth_matches", return_value=True):
            status, body = asgi_call(
                self.admin_main.app, "POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"},
                {"origin": "http://test"},
            )
        self.assertEqual(status, 202, body)
        self.assertEqual(client.calls, [("POST", "/internal/backups/lifecycle/apply"), ("GET", "/internal/backups")])
        payload = json.loads(body)
        return payload["state"], payload["detail"]

    def test_connection_reset_after_sending_asks_the_scheduler_too(self) -> None:
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        real_getresponse = http.client.HTTPConnection.getresponse

        def reset_for_apply(connection):
            if connection._method == "POST":
                bridge.apply_finished.wait(HANG_TIMEOUT_SECONDS)
                raise ConnectionResetError("reset")
            return real_getresponse(connection)

        with patch.object(http.client.HTTPConnection, "getresponse", reset_for_apply):
            status, payload = self.apply(token)

        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, {"state": "cleanup_not_running", "detail": CLEANUP_NOT_RUNNING})

    def test_timeout_before_the_request_is_sent_means_nothing_started(self) -> None:
        older, newest, scheduler = self.two_local_fulls()
        for stage in ("connect", "send"):
            with self.subTest(stage=stage):
                bridge_path = self.socket_path
                if bridge_path.exists():
                    bridge_path.unlink()
                bridge = self.bridge(scheduler)
                token = self.plan_token(bridge)

                def send_times_out(connection, _data):
                    if connection.sock is None:
                        connection.connect()  # connected, then the request bytes time out
                    raise TimeoutError("timed out")

                stalled = (
                    patch.object(self.client_module._UnixConnection, "connect", side_effect=TimeoutError("timed out"))
                    if stage == "connect"
                    else patch.object(self.client_module._UnixConnection, "send", send_times_out)
                )
                with stalled:
                    status, payload = self.apply(token)

                # The request never reached the scheduler in full: nothing ran,
                # the plan is unused and no status read is needed.
                self.assertEqual(status, 503, payload)
                self.assertEqual(payload["detail"], "The backup scheduler service is not available.")
                self.assertEqual(bridge.seen, [])
                self.assertIn(token, scheduler._plans)
                self.assertTrue((self._paths.local_dir / older.name).exists())
                bridge.close()


if __name__ == "__main__":
    unittest.main()
