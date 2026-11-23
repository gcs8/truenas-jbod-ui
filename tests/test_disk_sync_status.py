"""Synthetic regressions for disk sync admission and remembered-job status."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.domain import DiskInventorySyncMode
from app.services.inventory import DiskInventorySyncBusy, _parse_disk_inventory_sync_job
from app.services.parsers import ParsedSSHData, MultipathInfo, MultipathConsumer
from tests import test_disk_inventory_sync as sync_tests
from tests.test_disk_inventory_sync import (
    CORE_MIDCLT, RecordingSSHRunner, failed, job_payload, ok,
)


class DiskSyncStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = sync_tests.DiskInventorySyncServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.build_service(timeout_seconds=10)
        self.clock = [0.0]
        self.sleeps = []
        self.service._disk_inventory_sync_clock = lambda: self.clock[0]

        async def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock[0] += seconds
        self.service._disk_inventory_sync_sleep = sleep

    async def test_multipath_never_reads_or_clears_the_remembered_full_job(self):
        self.service._disk_inventory_sync_active_job_id = 17
        runner = RecordingSSHRunner([ok("null")])
        self.service._run_ssh_command = runner
        # Even an in-request full sync must not fence the separate operation.
        async with self.service._disk_inventory_sync_lock:
            result = await self.service.sync_disk_inventory(DiskInventorySyncMode.multipath)
        self.assertEqual(result.state, "SUCCESS")
        self.assertEqual(runner.commands, [f"sudo -n {CORE_MIDCLT} call disk.multipath_sync"])
        self.assertEqual(self.service._disk_inventory_sync_active_job_id, 17)

    async def test_previous_status_failure_retries_with_bounded_backoff_then_starts(self):
        self.service._disk_inventory_sync_active_job_id = 17
        runner = RecordingSSHRunner([failed("SSH command timed out.")] * 3 + [ok("18"), ok(job_payload(18, "SUCCESS"))])
        self.service._run_ssh_command = runner
        result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(result.state, "SUCCESS")
        self.assertEqual(result.job_id, 18)
        self.assertIn("previous disk sync job 17", result.message.lower())
        self.assertIn("unknown", result.message.lower())
        self.assertEqual(self.sleeps, [0.5, 1.0])
        self.assertTrue(all(0 < value <= 10 for value in runner.timeouts))
        self.assertEqual(sum("call disk.sync_all" in command for command in runner.commands), 1)

    async def test_recovered_status_refuses_only_when_job_is_known_active(self):
        self.service._disk_inventory_sync_active_job_id = 17
        runner = RecordingSSHRunner([failed("temporary status failure"), ok(job_payload(17, "RUNNING"))])
        self.service._run_ssh_command = runner
        with self.assertRaisesRegex(DiskInventorySyncBusy, "job 17.*running"):
            await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(self.sleeps, [0.5])
        self.assertFalse(any("call disk.sync_all" in command for command in runner.commands))

    async def test_slow_previous_reads_reserve_budget_for_start_and_new_status(self):
        self.service._disk_inventory_sync_active_job_id = 17
        timeouts = []
        commands = []

        async def run(command, **kwargs):
            commands.append(command)
            timeouts.append(kwargs.get("timeout_seconds"))
            if len(commands) <= 3:
                self.clock[0] += kwargs.get("timeout_seconds") or 15
                return failed("SSH command timed out.")
            return ok("18") if "call disk.sync_all" in command else ok(job_payload(18, "SUCCESS"))
        self.service._run_ssh_command = run
        result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(result.state, "SUCCESS")
        self.assertLessEqual(self.clock[0], 10)
        self.assertTrue(all(value is not None and value > 0 for value in timeouts))

    async def test_waiting_job_names_readable_running_blocker(self):
        self.service._disk_inventory_sync_active_job_id = 17
        self.service._run_ssh_command = RecordingSSHRunner([ok(job_payload(17, "WAITING"))])
        self.service.truenas_client.fetch_disk_sync_jobs = AsyncMock(return_value=[
            {"id": 16, "method": "disk.sync_all", "state": "RUNNING",
             "progress": {"percent": 20, "description": "Enumerating system disks"},
             "time_started": {"$date": 0}},
            {"id": 17, "method": "disk.sync_all", "state": "WAITING"},
            {"id": 15, "method": "pool.scrub", "state": "RUNNING"},
        ])
        with patch("time.time", return_value=1260):
            with self.assertRaises(DiskInventorySyncBusy) as caught:
                await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        message = str(caught.exception)
        self.assertIn("Waiting behind disk sync job 16", message)
        self.assertIn("running", message)
        self.assertIn("20%", message)
        self.assertIn("Enumerating system disks", message)
        self.assertIn("21 min", message)
        self.assertNotIn("pool.scrub", message)

    async def test_failed_job_error_is_bounded_and_free_of_controls(self):
        state, error = _parse_disk_inventory_sync_job(job_payload(17, "FAILED", "UNIQUE\x00\x1b\x7f\u202e\nconstraint " + "x" * 700), 17)
        self.assertEqual(state, "FAILED")
        self.assertLessEqual(len(error), 400)
        self.assertTrue(all(character.isprintable() for character in error))
        self.assertIn("constraint", error)

    def source_bundle(self, *, multipath=False, same_serial=True, same_lun=True):
        rows = [
            {"name": "da0", "serial": "SANITIZED-DISK-A", "lunid": "SANITIZED-LUN-A"},
            {"name": "da1", "serial": "SANITIZED-DISK-A" if same_serial else "SANITIZED-DISK-B",
             "lunid": "SANITIZED-LUN-A" if same_lun else "SANITIZED-LUN-B"},
        ]
        parsed = ParsedSSHData()
        if multipath:
            parsed.multipath_info["multipath/disk1"] = MultipathInfo(
                name="disk1", device_name="multipath/disk1", consumers=[MultipathConsumer("da0"), MultipathConsumer("da1")])
        self.service._source_bundle = SimpleNamespace(raw_data=SimpleNamespace(disks=rows), ssh_collected=True)
        self.service._parsed_ssh_data_for_enclosure = lambda *_args: parsed

    async def test_core_duplicate_paths_advise_multipath_before_starting_full_sync(self):
        self.source_bundle()
        runner = RecordingSSHRunner([ok("18"), ok(job_payload(18, "SUCCESS"))])
        self.service._run_ssh_command = runner
        result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(result.state, "MULTIPATH_REQUIRED")
        self.assertIn("Sync multipath table", result.message)
        self.assertIn("da0", result.message)
        self.assertEqual(runner.commands, [])

    async def test_duplicate_identity_controls_do_not_gate_full_sync(self):
        for kwargs in ({"multipath": True}, {"same_serial": False}, {"same_lun": False}):
            with self.subTest(kwargs=kwargs):
                self.source_bundle(**kwargs)
                self.service._run_ssh_command = RecordingSSHRunner([ok("18"), ok(job_payload(18, "SUCCESS"))])
                result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
                self.assertEqual(result.state, "SUCCESS")

    async def test_status_route_observes_completion_without_starting_another_job(self):
        from app import main, routes
        self.service._disk_inventory_sync_active_job_id = 17
        runner = RecordingSSHRunner([ok(job_payload(17, "SUCCESS"))])
        self.service._run_ssh_command = runner
        self.fixture.prime_cache(self.service)
        route = next((route for route in main.app.routes if getattr(route, "path", "") == "/api/systems/{system_id}/disk-inventory-sync" and "GET" in getattr(route, "methods", set())), None)
        self.assertIsNotNone(route, "a read-only status route must let the UI observe completion")
        registry = SimpleNamespace(has_system=lambda _id: True, get_service=lambda _id: self.service)
        with patch.object(routes, "get_inventory_registry", return_value=registry), patch.object(routes, "add_perf_metadata"):
            response = await route.endpoint(system_id="sync-contract", job_id=17)
        body = json.loads(response.body)
        self.assertEqual(body["state"], "SUCCESS")
        self.assertEqual(self.service._cache, {})
        self.assertEqual(len(runner.commands), 1)
        self.assertNotIn("disk.sync_all", runner.commands[0])

    async def test_status_failure_remains_unknown_not_failed_and_retains_job(self):
        self.service._disk_inventory_sync_active_job_id = 17
        runner = RecordingSSHRunner([failed("SSH command timed out.")] * 3)
        self.service._run_ssh_command = runner
        observer = getattr(self.service, "get_disk_inventory_sync_status", None)
        self.assertIsNotNone(observer, "read-only status must not re-submit a sync")
        result = await observer()
        self.assertEqual(result.state, "UNKNOWN")
        self.assertIn("unknown", result.message.lower())
        self.assertEqual(self.service._disk_inventory_sync_active_job_id, 17)
        self.assertFalse(any("call disk.sync_all" in command for command in runner.commands))

    async def test_expired_poll_budget_keeps_last_active_state_without_another_read(self):
        self.service.settings.app.disk_inventory_sync_timeout_seconds = 2
        for state in ("RUNNING", "WAITING"):
            with self.subTest(state=state):
                self.clock[0] = 0.0
                self.service._disk_inventory_sync_active_job_id = None
                commands = []

                async def run(command, **kwargs):
                    commands.append(command)
                    if command.endswith("call disk.sync_all"):
                        return ok("17")
                    self.clock[0] += 0.02
                    if kwargs["timeout_seconds"] < 0.02:
                        return failed("status read deadline expired")
                    return ok(job_payload(17, state))
                self.service._run_ssh_command = run
                result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
                self.assertEqual(result.state, state)
                self.assertTrue(result.timed_out)
                self.assertEqual(len(commands), 2, "deadline expiry must not issue a near-zero-budget read")
                self.assertEqual(self.service._disk_inventory_sync_active_job_id, 17)

    async def test_inflight_request_does_not_mislabel_its_waiting_job_running(self):
        self.service._disk_inventory_sync_active_job_id = 17
        self.service._run_ssh_command = RecordingSSHRunner([ok(job_payload(17, "WAITING"))])
        self.service.truenas_client.fetch_disk_sync_jobs = AsyncMock(return_value=[])
        async with self.service._disk_inventory_sync_lock:
            with self.assertRaises(DiskInventorySyncBusy) as caught:
                await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(caught.exception.result.state, "WAITING")
        self.assertIn("waiting", str(caught.exception).lower())

    async def test_waiting_without_readable_queue_context_does_not_claim_running(self):
        self.service._disk_inventory_sync_active_job_id = 17
        self.service._run_ssh_command = RecordingSSHRunner([ok(job_payload(17, "WAITING"))])
        self.service.truenas_client.fetch_disk_sync_jobs = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(DiskInventorySyncBusy) as caught:
            await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertIn("waiting", str(caught.exception).lower())
        self.assertNotIn("running", str(caught.exception).lower())
        self.assertEqual(caught.exception.result.state, "WAITING")

    async def test_default_status_read_allows_an_eight_second_response(self):
        self.service.settings.app.disk_inventory_sync_timeout_seconds = 180
        self.service._disk_inventory_sync_active_job_id = 17
        observed = []

        async def read(command, **kwargs):
            observed.append(kwargs["timeout_seconds"])
            self.assertGreaterEqual(kwargs["timeout_seconds"], 8)
            self.clock[0] += 8
            return ok(job_payload(17, "RUNNING"))
        self.service._run_ssh_command = read
        with self.assertRaises(DiskInventorySyncBusy):
            await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(len(observed), 1)

    async def test_cancelled_status_read_never_starts_a_replacement(self):
        import asyncio
        self.service._disk_inventory_sync_active_job_id = 17
        self.service._run_ssh_command = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.service._run_ssh_command.assert_awaited_once()
        self.assertEqual(self.service._disk_inventory_sync_active_job_id, 17)
        self.assertFalse(self.service._disk_inventory_sync_lock.locked())

    async def test_scale_does_not_gate_on_core_multipath_advisory(self):
        self.source_bundle()
        self.service.system.truenas.platform = "scale"
        self.service._run_ssh_command = RecordingSSHRunner([ok("18"), ok(job_payload(18, "SUCCESS"))])
        result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(result.state, "SUCCESS")

    async def test_missing_identity_and_duplicate_rows_are_not_multiple_disk_paths(self):
        self.source_bundle()
        rows = self.service._source_bundle.raw_data.disks
        rows[1]["name"] = "da0"
        runner = RecordingSSHRunner([ok("18"), ok(job_payload(18, "SUCCESS"))])
        self.service._run_ssh_command = runner
        result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(result.state, "SUCCESS")
        rows[1]["name"] = "da1"
        rows[1]["lunid"] = ""
        self.service._run_ssh_command = RecordingSSHRunner([ok("19"), ok(job_payload(19, "SUCCESS"))])
        result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertEqual(result.state, "SUCCESS")

    async def test_api_queue_reader_uses_only_bounded_read_only_get_jobs(self):
        from contextlib import asynccontextmanager
        from app.config import TrueNASConfig
        from app.services.truenas_ws import TrueNASWebsocketClient
        client = TrueNASWebsocketClient(TrueNASConfig())
        token = object()

        @asynccontextmanager
        async def session():
            yield token
        client._session = session
        client._call = AsyncMock(return_value=[{"id": index} for index in range(50)])
        result = await client.fetch_disk_sync_jobs()
        self.assertEqual(len(result), 32)
        client._call.assert_awaited_once_with(token, "core.get_jobs", [
            [["method", "=", "disk.sync_all"], ["state", "in", ["RUNNING", "WAITING"]]],
            {"limit": 32, "order_by": ["id"]},
        ])

    async def test_status_http_requires_operator_auth_and_preserves_failed_job_error(self):
        from app import routes
        from tests.test_read_ui_auth import build_app, invoke_asgi, basic_header
        application = build_app(auth_mode="basic", public_origin="https://ui.example.test")
        self.service._disk_inventory_sync_active_job_id = 17
        runner = RecordingSSHRunner([ok(job_payload(17, "FAILED", "UNIQUE constraint failed"))])
        self.service._run_ssh_command = runner
        registry = SimpleNamespace(has_system=lambda _id: True, get_service=lambda _id: self.service)
        target = "/api/systems/sync-contract/disk-inventory-sync?job_id=17"
        with patch.object(routes, "get_inventory_registry", return_value=registry):
            status, _headers, body = await invoke_asgi(application, target)
            self.assertEqual(status, 401)
            self.assertEqual(runner.commands, [])
            status, _headers, body = await invoke_asgi(
                application, target, authorization=basic_header("operator", "synthetic-passphrase"),
                origin="https://ui.example.test")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["state"], "FAILED")
        self.assertEqual(json.loads(body)["error"], "UNIQUE constraint failed")


if __name__ == "__main__":
    unittest.main()
