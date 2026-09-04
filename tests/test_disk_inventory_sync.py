from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import main as app_main
from app.config import AppConfig, Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import (
    DiskInventorySyncMode,
    DiskInventorySyncRequest,
    DiskInventorySyncResult,
    InventorySnapshot,
)
from app.services.inventory import (
    DiskInventorySyncBusy,
    DiskInventorySyncUnavailable,
    InventoryService,
)
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.ssh_probe import SSHCommandResult
from app.services.truenas_ws import TrueNASAPIError


CORE_MIDCLT = "/usr/local/bin/midclt"
SCALE_MIDCLT = "/usr/bin/midclt"


class RecordingSSHRunner:
    """Answer sudo midclt commands from a scripted queue and record what ran."""

    def __init__(self, responses: list[SSHCommandResult | Exception]) -> None:
        self.responses = list(responses)
        self.commands: list[str] = []

    async def __call__(self, command: str, host: str | None = None) -> SSHCommandResult:
        self.commands.append(command)
        if not self.responses:
            raise AssertionError(f"unexpected extra SSH command: {command}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SSHCommandResult(
            command=command,
            ok=response.ok,
            stdout=response.stdout,
            stderr=response.stderr,
            exit_code=response.exit_code,
        )


def ok(stdout: str = "") -> SSHCommandResult:
    return SSHCommandResult(command="", ok=True, stdout=stdout, exit_code=0)


def failed(stderr: str, exit_code: int = 1) -> SSHCommandResult:
    return SSHCommandResult(command="", ok=False, stderr=stderr, exit_code=exit_code)


def job_payload(job_id: int, state: str, error: str | None = None) -> str:
    return json.dumps(
        [
            {
                "id": job_id,
                "method": "disk.sync_all",
                "state": state,
                "error": error,
                "progress": {"percent": 100 if state == "SUCCESS" else 40},
                "result": None,
            }
        ]
    )


class DiskInventorySyncServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)

    def build_service(
        self,
        *,
        platform: str = "core",
        ssh_enabled: bool = True,
        timeout_seconds: int = 180,
    ) -> InventoryService:
        settings = Settings(app=AppConfig(disk_inventory_sync_timeout_seconds=timeout_seconds))
        system = SystemConfig(
            id="sync-contract",
            label="Synthetic TrueNAS",
            truenas=TrueNASConfig(platform=platform),
            ssh=SSHConfig(enabled=ssh_enabled, host="truenas.invalid", user="jbodmap", commands=[]),
        )
        return InventoryService(
            settings,
            system,
            AsyncMock(),
            AsyncMock(),
            None,
            MappingStore(str(Path(self._temp.name) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(self._temp.name) / "slot_detail_cache.json")),
        )

    @staticmethod
    def prime_cache(service: InventoryService) -> None:
        snapshot = InventorySnapshot(slots=[], refresh_interval_seconds=30)
        service._cache["__default__"] = snapshot
        service._cache["enc-a"] = snapshot

    async def test_multipath_mode_runs_only_the_core_multipath_sync_command(self) -> None:
        service = self.build_service(platform="core")
        runner = RecordingSSHRunner([ok("null\n")])
        service._run_ssh_command = runner
        self.prime_cache(service)

        result = await service.sync_disk_inventory(DiskInventorySyncMode.multipath)

        self.assertEqual(runner.commands, [f"sudo -n {CORE_MIDCLT} call disk.multipath_sync"])
        self.assertEqual(result.mode, DiskInventorySyncMode.multipath)
        self.assertEqual(result.state, "SUCCESS")
        self.assertIsNone(result.job_id)
        self.assertFalse(result.timed_out)
        self.assertIn("Refresh to see the updated bays", result.message)
        self.assertEqual(service._cache, {}, "a successful sync must drop the cached inventory")

    async def test_multipath_failure_is_reported_as_a_bounded_sentence(self) -> None:
        service = self.build_service(platform="core")
        service._run_ssh_command = RecordingSSHRunner(
            [failed("sudo: a password is required\n" + "x" * 900)]
        )
        self.prime_cache(service)

        with self.assertRaises(TrueNASAPIError) as caught:
            await service.sync_disk_inventory(DiskInventorySyncMode.multipath)

        message = str(caught.exception)
        self.assertTrue(message.startswith("TrueNAS could not rebuild its multipath table: "), message)
        self.assertLess(len(message), 500)
        self.assertTrue(message.endswith("..."), message)
        self.assertNotEqual(service._cache, {}, "a failed sync must not drop the cached inventory")

    async def test_full_mode_parses_the_job_id_and_polls_until_success(self) -> None:
        service = self.build_service(platform="scale")
        runner = RecordingSSHRunner(
            [
                ok("268071\n"),
                ok(job_payload(268071, "RUNNING")),
                ok(job_payload(268071, "SUCCESS")),
            ]
        )
        service._run_ssh_command = runner
        clock = [1000.0]
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        service._disk_inventory_sync_clock = lambda: clock[0]
        service._disk_inventory_sync_sleep = fake_sleep
        self.prime_cache(service)

        result = await service.sync_disk_inventory(DiskInventorySyncMode.full)

        expected_poll = f"sudo -n {SCALE_MIDCLT} call core.get_jobs '[[\"id\",\"=\",268071]]'"
        self.assertEqual(
            runner.commands,
            [f"sudo -n {SCALE_MIDCLT} call disk.sync_all", expected_poll, expected_poll],
        )
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(result.state, "SUCCESS")
        self.assertEqual(result.job_id, 268071)
        self.assertEqual(result.elapsed_seconds, 2.0)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.error)
        self.assertEqual(result.message, "TrueNAS re-read its disk inventory. Refresh to see the updated bays.")
        self.assertEqual(service._cache, {})

    async def test_full_mode_uses_the_core_midclt_path_on_core(self) -> None:
        service = self.build_service(platform="core")
        runner = RecordingSSHRunner([ok("42"), ok(job_payload(42, "SUCCESS"))])
        service._run_ssh_command = runner

        result = await service.sync_disk_inventory(DiskInventorySyncMode.full)

        self.assertEqual(result.job_id, 42)
        self.assertTrue(all(command.startswith(f"sudo -n {CORE_MIDCLT} call ") for command in runner.commands))

    async def test_full_mode_honours_the_timeout_with_a_clear_message(self) -> None:
        service = self.build_service(platform="core", timeout_seconds=180)
        runner = RecordingSSHRunner(
            [
                ok("268071"),
                ok(job_payload(268071, "RUNNING")),
                ok(job_payload(268071, "RUNNING")),
                ok(job_payload(268071, "RUNNING")),
            ]
        )
        service._run_ssh_command = runner
        clock = [0.0]

        async def fake_sleep(seconds: float) -> None:
            clock[0] += 100.0

        service._disk_inventory_sync_clock = lambda: clock[0]
        service._disk_inventory_sync_sleep = fake_sleep
        self.prime_cache(service)

        result = await service.sync_disk_inventory(DiskInventorySyncMode.full)

        # start + poll@0s + poll@100s + poll@200s (timeout reached, no further poll)
        self.assertEqual(len(runner.commands), 4)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.state, "RUNNING")
        self.assertEqual(result.job_id, 268071)
        self.assertEqual(result.elapsed_seconds, 200.0)
        self.assertEqual(
            result.message,
            "TrueNAS is still running disk sync job 268071 after 200 s. "
            "Check the job in the TrueNAS UI, then refresh here when it finishes.",
        )
        self.assertNotEqual(service._cache, {}, "a timed-out sync must not claim the inventory changed")

    async def test_full_mode_reports_a_failed_job_with_its_middleware_error(self) -> None:
        service = self.build_service(platform="core")
        service._run_ssh_command = RecordingSSHRunner(
            [ok("7"), ok(job_payload(7, "FAILED", "[EFAULT] disk sync failed\n  second line"))]
        )
        self.prime_cache(service)

        result = await service.sync_disk_inventory(DiskInventorySyncMode.full)

        self.assertEqual(result.state, "FAILED")
        self.assertEqual(result.error, "[EFAULT] disk sync failed second line")
        self.assertEqual(result.message, "TrueNAS reported disk sync job 7 failed.")
        self.assertNotEqual(service._cache, {})

    async def test_full_mode_refuses_when_no_job_id_comes_back(self) -> None:
        service = self.build_service(platform="core")
        service._run_ssh_command = RecordingSSHRunner([ok("null")])

        with self.assertRaises(TrueNASAPIError) as caught:
            await service.sync_disk_inventory(DiskInventorySyncMode.full)

        self.assertIn("did not return a job id", str(caught.exception))

    async def test_unsupported_combinations_refuse_before_running_anything(self) -> None:
        runner = RecordingSSHRunner([])

        scale = self.build_service(platform="scale")
        scale._run_ssh_command = runner
        with self.assertRaises(DiskInventorySyncUnavailable) as caught:
            await scale.sync_disk_inventory(DiskInventorySyncMode.multipath)
        self.assertEqual(str(caught.exception), "Multipath table sync is only available on TrueNAS CORE.")

        linux = self.build_service(platform="linux")
        linux._run_ssh_command = runner
        with self.assertRaises(DiskInventorySyncUnavailable) as caught:
            await linux.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertIn("unsupported on this platform", str(caught.exception))

        ssh_off = self.build_service(platform="core", ssh_enabled=False)
        ssh_off._run_ssh_command = runner
        with self.assertRaises(DiskInventorySyncUnavailable) as caught:
            await ssh_off.sync_disk_inventory(DiskInventorySyncMode.full)
        self.assertIn("SSH is disabled for this system", str(caught.exception))

        self.assertEqual(runner.commands, [])

    async def test_concurrent_sync_for_the_same_system_is_refused(self) -> None:
        service = self.build_service(platform="core")
        release = asyncio.Event()
        commands: list[str] = []

        async def blocking_runner(command: str, host: str | None = None) -> SSHCommandResult:
            commands.append(command)
            await release.wait()
            return ok("null")

        service._run_ssh_command = blocking_runner

        first = asyncio.create_task(service.sync_disk_inventory(DiskInventorySyncMode.multipath))
        await asyncio.sleep(0)
        self.assertEqual(len(commands), 1)

        with self.assertRaises(DiskInventorySyncBusy):
            await service.sync_disk_inventory(DiskInventorySyncMode.multipath)

        release.set()
        result = await first
        self.assertEqual(result.state, "SUCCESS")
        self.assertEqual(len(commands), 1, "the refused request must not reach SSH")

        # Once the first sync finishes the lock is free again.
        service._run_ssh_command = RecordingSSHRunner([ok("null")])
        second = await service.sync_disk_inventory(DiskInventorySyncMode.multipath)
        self.assertEqual(second.state, "SUCCESS")


class DiskInventorySyncRouteTests(unittest.TestCase):
    def route(self):
        return next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/systems/{system_id}/disk-inventory-sync"
            and "POST" in (getattr(route, "methods", None) or set())
        )

    def invoke(self, service: Mock, payload: DiskInventorySyncRequest):
        registry = Mock()
        registry.get_service.return_value = service
        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            return asyncio.run(self.route().endpoint(system_id="system-a", payload=payload)), registry

    @staticmethod
    def build_service(platform: str = "core") -> Mock:
        service = Mock()
        service.system.id = "system-a"
        service.system.truenas.platform = platform
        return service

    def test_route_requires_confirm_before_touching_the_service(self) -> None:
        service = self.build_service()
        service.sync_disk_inventory = AsyncMock()

        with self.assertRaises(HTTPException) as caught:
            self.invoke(service, DiskInventorySyncRequest(mode=DiskInventorySyncMode.full, confirm=False))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "Confirm the TrueNAS disk inventory sync before running it.")
        service.sync_disk_inventory.assert_not_awaited()

    def test_route_maps_unavailable_to_400_and_busy_to_409(self) -> None:
        service = self.build_service("scale")
        service.sync_disk_inventory = AsyncMock(
            side_effect=DiskInventorySyncUnavailable("Multipath table sync is only available on TrueNAS CORE.")
        )
        with self.assertRaises(HTTPException) as caught:
            self.invoke(service, DiskInventorySyncRequest(mode=DiskInventorySyncMode.multipath, confirm=True))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "Multipath table sync is only available on TrueNAS CORE.")

        service.sync_disk_inventory = AsyncMock(side_effect=DiskInventorySyncBusy("busy sentence"))
        with self.assertRaises(HTTPException) as caught:
            self.invoke(service, DiskInventorySyncRequest(mode=DiskInventorySyncMode.full, confirm=True))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "busy sentence")

    def test_route_returns_only_the_bounded_result_fields(self) -> None:
        service = self.build_service()
        service.sync_disk_inventory = AsyncMock(
            return_value=DiskInventorySyncResult(
                mode=DiskInventorySyncMode.full,
                state="SUCCESS",
                job_id=268071,
                elapsed_seconds=12.0,
                message="TrueNAS re-read its disk inventory. Refresh to see the updated bays.",
            )
        )

        response, registry = self.invoke(
            service, DiskInventorySyncRequest(mode=DiskInventorySyncMode.full, confirm=True)
        )

        registry.get_service.assert_called_once_with("system-a")
        service.sync_disk_inventory.assert_awaited_once_with(DiskInventorySyncMode.full)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(
            set(body),
            {"mode", "state", "job_id", "elapsed_seconds", "timed_out", "error", "message"},
        )
        self.assertEqual(body["mode"], "full")
        self.assertEqual(body["job_id"], 268071)
        self.assertEqual(body["state"], "SUCCESS")

    def test_request_model_rejects_unknown_modes(self) -> None:
        with self.assertRaises(ValueError):
            DiskInventorySyncRequest.model_validate({"mode": "everything", "confirm": True})


if __name__ == "__main__":
    unittest.main()
