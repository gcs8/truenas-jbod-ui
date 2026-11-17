"""Retention keeper checks stay bounded and fail closed (#1188).

Before a retention deletion, grooming reads back the newest kept copy and stops
as soon as one kept copy matches. A missing or mismatching keeper is downgraded
and the next one is tried, but only a small fixed number of reads per class and
location; running out of reads refuses the deletions. Synthetic files only: the
remote target is the real filesystem transport over a temporary directory.
"""

from __future__ import annotations

import contextlib
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

    def test_sidecar_removal_log_counts_one_full_copy_in_the_singular(self) -> None:
        scheduler = self.make({"full": {"enabled": True}})
        backup_dir = self._paths.history_backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        for age in range(14, 0, -1):
            created = self.now - timedelta(days=age)
            path = backup_dir / f"history-{created:%Y%m%dT%H%M%S}Z.sqlite3"
            path.write_bytes(f"sidecar {age}".encode())
            os.utime(path, (created.timestamp(), created.timestamp()))

        with self.assertLogs(scheduler_service.logger, level="INFO") as logs:
            scheduler.run_now("full")

        removal = [line for line in logs.output if "replaced history sidecar" in line]
        self.assertEqual(len(removal), 1, logs.output)
        self.assertIn("Removed 1 replaced history sidecar backup copy; 1 digest-checked local FULL copy remains.",
                      removal[0])

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
            body = CLEANUP_RESULT_BODY
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


# The one thing the admin apply route says when no usable answer to a clean-up
# came back, whatever the reason. It asks the scheduler nothing more.
CLEANUP_STATE_UNKNOWN = (
    "No answer came back for this clean-up. It may have run, may still be running, or may not have "
    "started. Reload this page in a few minutes and check Last clean-up before starting another."
)
NO_ANSWER = {"state": "cleanup_state_unknown", "detail": CLEANUP_STATE_UNKNOWN}
NOT_AVAILABLE = "The backup scheduler service is not available."
CLEANUP_RESULT_BODY = (
    b'{"ok": true, "deleted": [], "already_missing": [], "failed": null, "not_attempted": [], '
    b'"downgraded": [], "kept_after_replan": []}'
)
ROOT = Path(__file__).resolve().parents[1]


def _apply_deadline(module, seconds: float):
    """A scaled-down apply deadline."""

    return patch.object(module, "LIFECYCLE_APPLY_DEADLINE_SECONDS", seconds)


class AdminCleanupApplyWaitTests(unittest.TestCase):
    """A manual clean-up apply gets its own deadline; a missing answer is never read as a state."""

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

    def test_apply_deadline_leaves_margin_inside_the_page_timer(self) -> None:
        apply_deadline = self.client_module.LIFECYCLE_APPLY_DEADLINE_SECONDS
        self.assertFalse(hasattr(self.client_module, "CLEANUP_STATUS_READ_DEADLINE_SECONDS"))
        source = (ROOT / "admin_service/static/admin_backups.js").read_text()
        self.assertIn("const LONG_TIMEOUT_MS = 15 * 60 * 1000;", source)
        page_seconds = 15 * 60
        self.assertGreater(apply_deadline, self.client_module.DEFAULT_TIMEOUT_SECONDS)
        # The admin service answers at least a minute before the page gives up.
        self.assertLessEqual(apply_deadline + 60, page_seconds)

    def test_late_answer_is_reported_as_not_known(self) -> None:
        late = _LateScheduler(self.socket_path)
        self.addCleanup(late.close)
        client = self.client_module.BackupSchedulerClient(str(self.socket_path), timeout=0.5)
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), \
                _apply_deadline(self.client_module, 0.5):
            status, body = self.call("POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"})

        self.assertTrue(late.requests and late.requests[0].startswith(b"POST /internal/backups/lifecycle/apply"))
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body), NO_ANSWER)

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

    def assert_not_sent(self, status, body) -> None:
        payload = json.loads(body)
        self.assertEqual(status, 503, payload)
        self.assertRegex(str(payload.get("request_id")), r"^[0-9a-f]{32}$")
        self.assertEqual(
            payload, {"ok": False, "state": "cleanup_not_sent", "detail": NOT_AVAILABLE, "request_id": payload["request_id"]}
        )

    def test_a_clean_up_that_was_never_sent_says_so_in_a_fixed_state(self) -> None:
        # No connection opened, so no byte left. The 503 carries a fixed state
        # the page can tell apart from a gateway's 503, at each such stage.
        import socket

        bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(bound.close)
        bound.bind(str(self.socket_path))  # a socket file nobody listens on
        missing = self.client_module.BackupSchedulerClient(str(Path(self.temp.name) / "gone.sock"))
        present = self.client_module.BackupSchedulerClient(str(self.socket_path))
        never_connect = patch.object(self.client_module._UnixConnection, "connect", side_effect=AssertionError("connect"))
        connect_times_out = patch.object(self.client_module._UnixConnection, "connect", side_effect=TimeoutError("timed out"))
        stages = {
            "socket file missing": (missing, never_connect),
            "connect refused": (present, contextlib.nullcontext()),
            "connect timed out": (present, connect_times_out),
            "deadline passed before connect": (present, never_connect),
        }
        for name, (client, connect) in stages.items():
            deadline = _apply_deadline(self.client_module, 0.0 if "deadline" in name else 5.0)
            with self.subTest(name), connect, deadline, patch.object(
                self.admin_routes, "get_backup_scheduler_client", return_value=client
            ):
                status, body = self.call("POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"})
                self.assert_not_sent(status, body)

    def test_other_scheduler_routes_keep_their_not_available_answer_byte_for_byte(self) -> None:
        from admin_service import route_support

        client = self.client_module.BackupSchedulerClient(str(self.socket_path))  # no socket file
        request_id = "0123456789abcdef0123456789abcdef"
        expected = (
            b'{"ok":false,"detail":"The backup scheduler service is not available.","request_id":"'
            + request_id.encode()
            + b'"}'
        )
        siblings = (
            ("POST", "/api/admin/backups/run", {"backup_class": "full"}),
            ("GET", "/api/admin/backups/lifecycle/plan", None),
            ("POST", "/api/admin/backups/targets/t1/test", None),
            ("POST", "/api/admin/backups/b1/verify", None),
        )
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), patch.object(
            route_support, "admin_request_id", return_value=request_id
        ):
            for method, path, body in siblings:
                with self.subTest(path):
                    self.assertEqual(self.call(method, path, body), (503, expected))

    def test_page_uses_the_same_words_as_the_route(self) -> None:
        import re

        page = (ROOT / "admin_service/static/admin_backups.js").read_text()
        model = (ROOT / "admin_service/static/admin_backup_model.js").read_text()
        route = (ROOT / "admin_service/routes.py").read_text()
        # Join the "..." + "..." (page) and adjacent "..." "..." (route) string pieces.
        joined_page = re.sub(r'"\s*\+\s*"', "", " ".join(page.split()))
        joined_route = re.sub(r'"\s+"', "", " ".join(route.split()))
        self.assertTrue(f'"{CLEANUP_STATE_UNKNOWN}"' in joined_page)
        self.assertTrue(f'"{CLEANUP_STATE_UNKNOWN}"' in joined_route)
        # One message: no running, not-running or settled state is left after a lost answer.
        for name, source in (("page", page), ("model", model), ("route", route)):
            for gone in ("cleanup_running", "cleanup_not_running", "no longer running"):
                with self.subTest(source=name, gone=gone):
                    self.assertFalse(gone in " ".join(source.split()), gone)


class _SchedulerBridge:
    """Serves the real scheduler app over a Unix socket, one thread per connection.

    It stands in for the scheduler's HTTP server so a test can hold the body of
    an apply answer after the scheduler has really finished (and released its
    job reservation).
    """

    def __init__(self, path: Path, app) -> None:
        import socket

        self.app = app
        self.release = threading.Event()
        self.hold_apply_body = False
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
    """When no usable answer to a clean-up arrives, the route says it is not known and asks nothing more.

    The real admin route, the real Unix-socket client and the real scheduler app
    and scheduler; only the socket server in between can hold part of an answer.
    """

    APPLY_WAIT = 0.4

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

    def status_reads(self, bridge: _SchedulerBridge) -> int:
        return [path for _method, path in bridge.seen].count("/internal/backups")

    def apply(self, token: str):
        client = self.client_module.BackupSchedulerClient(str(self.socket_path), timeout=2.0)
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), \
                _apply_deadline(self.client_module, self.APPLY_WAIT), \
                patch.object(self.admin_main, "_request_origin_allowed", return_value=True), \
                patch.object(self.admin_main, "_basic_auth_matches", return_value=True):
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

    def test_answer_body_lost_after_a_finished_success_is_not_known(self) -> None:
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        bridge.hold_apply_body = True

        status, payload = self.apply(self.plan_token(bridge))

        self.assertTrue(bridge.apply_finished.is_set())
        self.assertIsNone(scheduler.running)
        grooming = scheduler.library()["grooming"]
        self.assertTrue(grooming["ok"])
        self.assertEqual(grooming["deleted"], 1)
        # Nothing runs, but that is not said: no result was received.
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, NO_ANSWER)
        self.assertEqual(self.status_reads(bridge), 0)

    def test_answer_body_lost_after_a_finished_failure_is_not_known(self) -> None:
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
        self.assertEqual(payload, NO_ANSWER)
        self.assertEqual(self.status_reads(bridge), 0)

    def test_answer_headers_late_while_the_scheduler_runs_the_clean_up(self) -> None:
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        gate, entered = self.hold_lifecycle_apply()

        status, payload = self.apply(token)

        self.assertTrue(entered.is_set())
        # The scheduler is running it, but the route does not ask and does not say.
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, NO_ANSWER)
        self.assertEqual(self.status_reads(bridge), 0)
        self.assertEqual(scheduler.running["backup_class"], "lifecycle")
        gate.set()
        self.assertTrue(bridge.apply_finished.wait(HANG_TIMEOUT_SECONDS))
        self.assertFalse((self._paths.local_dir / older.name).exists())

    def test_accepted_clean_up_that_has_not_reserved_yet_is_not_known(self) -> None:
        # The plan token is used, but the job is not reserved yet; the clean-up still deletes later.
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        gate = threading.Event()
        entered = threading.Event()
        real_reserve = scheduler._reserve

        def stalled(label):
            entered.set()
            gate.wait(HANG_TIMEOUT_SECONDS)
            return real_reserve(label)

        with patch.object(scheduler, "_reserve", stalled):
            try:
                status, payload = self.apply(token)
                self.assertTrue(entered.is_set())
                self.assertIsNone(scheduler.running)
                self.assertNotIn(token, scheduler._plans)
                self.assertTrue((self._paths.local_dir / older.name).exists())
                self.assertEqual(status, 202, payload)
                self.assertEqual(payload, NO_ANSWER)
                self.assertEqual(self.status_reads(bridge), 0)
            finally:
                gate.set()
            self.assertTrue(bridge.apply_finished.wait(HANG_TIMEOUT_SECONDS))
        # The text said copies may still be deleted, and one was.
        self.assertFalse((self._paths.local_dir / older.name).exists())

    def route_with(self, apply_answer):
        """Drive the route with a client whose apply call gives ``apply_answer`` (raised if an exception)."""

        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method, path, **_kwargs):
                self.calls.append((method, path))
                if path == "/internal/backups/lifecycle/apply":
                    if isinstance(apply_answer, BaseException):
                        raise apply_answer
                    return apply_answer
                raise AssertionError(f"unexpected scheduler request {method} {path}")

        client = Client()
        with patch.object(self.admin_routes, "get_backup_scheduler_client", return_value=client), patch.object(
            self.admin_main, "_request_origin_allowed", return_value=True
        ), patch.object(self.admin_main, "_basic_auth_matches", return_value=True):
            status, body = asgi_call(
                self.admin_main.app, "POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"},
                {"origin": "http://test"},
            )
        return status, json.loads(body), client.calls

    def test_only_the_expected_result_or_a_known_refusal_is_an_answer(self) -> None:
        from admin_service.services.backup_scheduler_client import SchedulerResponse

        result = json.loads(CLEANUP_RESULT_BODY)
        # The scheduler's own refusals before anything starts pass through, with no status read.
        for code, detail in ((400, "Request body must be a JSON object."),
                             (409, "The grooming plan expired or was already used; preview it again."),
                             (409, "A backup or grooming run is already in progress.")):
            with self.subTest(refusal=code, detail=detail[:20]):
                status, payload, calls = self.route_with(SchedulerResponse(status=code, payload={"detail": detail}))
                self.assertEqual((status, payload["detail"]), (code, detail))
                self.assertEqual(len(calls), 1)
        with self.subTest("the expected result"):
            status, payload, calls = self.route_with(SchedulerResponse(status=200, payload=result))
            self.assertEqual((status, payload), (200, result))
            self.assertEqual(len(calls), 1)
        unusable = {
            "unavailable after delivery": SchedulerResponse(status=503, payload={"detail": "not available"}),
            "server error": SchedulerResponse(status=500, payload={"detail": "boom"}),
            "not found": SchedulerResponse(status=404, payload={"detail": "Not Found"}),
            "bad gateway": SchedulerResponse(status=502, payload={"detail": "x"}),
            "gateway timeout": SchedulerResponse(status=504, payload={"detail": "x"}),
            "another success code": SchedulerResponse(status=202, payload=result),
            "empty body": SchedulerResponse(status=200, payload={}),
            "not an object": SchedulerResponse(status=200, payload=[]),
            "result without lists": SchedulerResponse(status=200, payload={"ok": True}),
            "deleted not a list": SchedulerResponse(status=200, payload={**result, "deleted": "all"}),
            "ok not a flag": SchedulerResponse(status=200, payload={**result, "ok": "yes"}),
            "failed not an object": SchedulerResponse(status=200, payload={**result, "failed": "x"}),
            "refusal without words": SchedulerResponse(status=409, payload={}),
            "refusal not an object": SchedulerResponse(status=400, payload=["x"]),
        }
        lost = {
            "no answer in time": self.client_module.SchedulerTimeoutError("no answer"),
            "no usable answer": self.client_module.SchedulerNoAnswerError("no answer"),
        }
        for name, answer in {**unusable, **lost}.items():
            with self.subTest(name):
                status, payload, calls = self.route_with(answer)
                self.assertEqual(status, 202, payload)
                self.assertEqual(payload, NO_ANSWER)
                # One request: nothing is asked after the apply.
                self.assertEqual(calls, [("POST", "/internal/backups/lifecycle/apply")])

    def test_wrong_status_after_delivery_is_not_known(self) -> None:
        # The real request reaches the real scheduler, whose clean-up is held
        # before deleting; the answer that comes back is a 503 with valid JSON.
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        gate, entered = self.hold_lifecycle_apply()
        real_getresponse = http.client.HTTPConnection.getresponse

        class Unavailable:
            status = 503

            def read(self, *_args):
                return b'{"detail": "The backup scheduler service is not available."}'

        def wrong_status(connection):
            if connection._method == "POST":
                self.assertTrue(entered.wait(HANG_TIMEOUT_SECONDS))
                return Unavailable()
            return real_getresponse(connection)

        try:
            with patch.object(http.client.HTTPConnection, "getresponse", wrong_status):
                status, payload = self.apply(token)
            self.assertEqual(status, 202, payload)
            self.assertEqual(payload, NO_ANSWER)
            self.assertEqual(self.status_reads(bridge), 0)
        finally:
            gate.set()
        self.assertTrue(bridge.apply_finished.wait(HANG_TIMEOUT_SECONDS))
        self.assertFalse((self._paths.local_dir / older.name).exists())

    def test_connection_reset_after_sending_is_not_known(self) -> None:
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
        self.assertEqual(payload, NO_ANSWER)
        self.assertEqual(self.status_reads(bridge), 0)

    def test_connect_timeout_means_nothing_started(self) -> None:
        # The connection never opened, so no byte left: that alone is still "not available".
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        with patch.object(self.client_module._UnixConnection, "connect", side_effect=TimeoutError("timed out")):
            status, payload = self.apply(token)
        self.assertEqual(status, 503, payload)
        self.assertEqual((payload.get("state"), payload.get("detail")), ("cleanup_not_sent", NOT_AVAILABLE))
        self.assertEqual(bridge.seen, [])
        self.assertIn(token, scheduler._plans)
        self.assertTrue((self._paths.local_dir / older.name).exists())

    def test_a_deadline_that_passes_after_connect_but_before_the_request_is_not_sent(self) -> None:
        # The connection opens, but the deadline passes before the client holds
        # the socket, so it never makes the request: nothing was sent.
        import time

        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        real_connect = self.client_module._UnixConnection.connect
        real_request = http.client.HTTPConnection.request
        requests: list[tuple] = []

        def slow_connect(connection):
            real_connect(connection)
            time.sleep(self.APPLY_WAIT + 0.3)

        def counted(connection, *args, **kwargs):
            requests.append(args[:2])
            return real_request(connection, *args, **kwargs)

        with patch.object(self.client_module._UnixConnection, "connect", slow_connect), patch.object(
            http.client.HTTPConnection, "request", counted
        ):
            status, payload = self.apply(token)
        self.assertEqual(requests, [])
        self.assertEqual(status, 503, payload)
        self.assertEqual((payload.get("state"), payload.get("detail")), ("cleanup_not_sent", NOT_AVAILABLE))
        self.assertEqual(bridge.seen, [])
        self.assertIn(token, scheduler._plans)
        self.assertTrue((self._paths.local_dir / older.name).exists())

    def send_that_fails_once_connected(self, whole: bool) -> None:
        # A failed send does not show how much the scheduler received. Withhold
        # the body's last byte (nothing can start), or send it all and wait
        # until the real clean-up has deleted the older copy: either way the
        # send then times out, and the route says the same thing.
        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        real_send = http.client.HTTPConnection.send
        sent: list[bytes] = []

        def failing(connection, data):
            if isinstance(data, bytes) and data.startswith(b"{"):
                real_send(connection, data if whole else data[:-1])
                sent.append(data if whole else data[:-1])
                if whole:
                    self.assertTrue(bridge.apply_finished.wait(HANG_TIMEOUT_SECONDS))
                raise TimeoutError("timed out")
            sent.append(data)
            return real_send(connection, data)

        with patch.object(self.client_module._UnixConnection, "send", failing):
            status, payload = self.apply(token)

        self.assertTrue(sent and sent[0].startswith(b"POST /internal/backups/lifecycle/apply"))
        self.assertEqual(sent[-1], json.dumps({"plan_token": token}).encode()[: None if whole else -1])
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, NO_ANSWER)
        self.assertEqual(self.status_reads(bridge), 0)
        self.assertEqual(token in scheduler._plans, not whole)
        self.assertEqual((self._paths.local_dir / older.name).exists(), not whole)

    def test_a_send_cut_short_once_connected_is_not_known(self) -> None:
        self.send_that_fails_once_connected(whole=False)

    def test_a_send_that_fails_after_the_whole_request_went_out_is_not_known(self) -> None:
        self.send_that_fails_once_connected(whole=True)

    def test_the_apply_wait_is_an_elapsed_deadline_not_a_socket_timeout(self) -> None:
        # The answer headers come late, just inside one socket timeout, and the
        # body never comes: the admin must still answer by its deadline,
        # inside the page's timer scaled by the same factor, asking nothing more.
        import asyncio
        import time

        older, newest, scheduler = self.two_local_fulls()
        bridge = self.bridge(scheduler)
        token = self.plan_token(bridge)
        real_app = bridge.app

        async def late_headers(scope, receive, send):
            async def delayed(message):
                if scope["path"].endswith("/lifecycle/apply") and message["type"] == "http.response.start":
                    await asyncio.sleep(0.6)
                await send(message)

            await real_app(scope, receive, delayed)

        bridge.app = late_headers
        bridge.hold_apply_body = True
        page_timer = 0.8 * 900 / 600  # the page's 15 minutes at the apply deadline's scale
        with patch.object(self, "APPLY_WAIT", 0.8):
            started = time.monotonic()
            status, payload = self.apply(token)
            elapsed = time.monotonic() - started
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, NO_ANSWER)
        self.assertEqual(self.status_reads(bridge), 0)
        self.assertLess(elapsed, page_timer, f"admin answered after {elapsed:.3f} s")
        self.assertGreaterEqual(elapsed, 0.8)

    def test_library_reads_running_together_with_the_stored_clean_up_result(self) -> None:
        # A library read that spans the end of a clean-up must not pair the
        # earlier stored result with the later idle state.
        older, newest, scheduler = self.two_local_fulls()
        before = scheduler.library()["grooming"]
        token = scheduler.plan()[0]
        gate, entered = self.hold_lifecycle_apply()
        worker = threading.Thread(target=scheduler.apply, args=(token,))
        worker.start()
        self.addCleanup(worker.join, HANG_TIMEOUT_SECONDS)
        self.assertTrue(entered.wait(HANG_TIMEOUT_SECONDS))
        paused = threading.Event()
        resume = threading.Event()
        self.addCleanup(resume.set)

        class SlowCoalescer:
            def poll(self_inner):
                paused.set()
                resume.wait(HANG_TIMEOUT_SECONDS)
                return 0

        scheduler.coalescer = SlowCoalescer()
        answers: list[dict] = []
        reader = threading.Thread(target=lambda: answers.append(scheduler.library()))
        reader.start()
        self.assertTrue(paused.wait(HANG_TIMEOUT_SECONDS))
        gate.set()
        worker.join(HANG_TIMEOUT_SECONDS)
        self.assertIsNone(scheduler.running)
        resume.set()
        reader.join(HANG_TIMEOUT_SECONDS)
        scheduler.coalescer = None

        answer = answers[0]
        self.assertEqual(answer["grooming"], before)
        # One moment: the clean-up was still running when its result was read.
        self.assertEqual((answer["running"] or {}).get("backup_class"), "lifecycle", answer["running"])


if __name__ == "__main__":
    unittest.main()
