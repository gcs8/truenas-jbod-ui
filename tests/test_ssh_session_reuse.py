"""SSH connection reuse and SSH SMART coalescing (#448).

Every reused connection must still come from ``SSHProbe._client`` (and so from
the same host-key policy), and the per-host lock must still keep other SSH
work on a host out while a planned session runs.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import paramiko

from tests.test_ssh_probe import _memory_streams

from app.config import AppConfig, Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import DiskInventorySyncMode, EnclosureOption, SlotView
from app.services.inventory import InventoryService, utcnow
from app.services.mapping_store import MappingStore
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.ssh_probe import (
    MAX_PARALLEL_CHANNELS_PER_CONNECTION,
    AutoPinHostKeyPolicy,
    SSHCommandResult,
    SSHProbe,
)

HOST = "192.0.2.44"
OTHER_HOST = "192.0.2.45"


def _exec_ok(delay: float = 0.0, counters: dict | None = None):
    lock = threading.Lock()

    def exec_command(command: str, timeout=None):
        if counters is not None:
            with lock:
                counters["open"] += 1
                counters["peak"] = max(counters["peak"], counters["open"])
        if delay:
            time.sleep(delay)
        streams = _memory_streams(f"{command} output".encode())
        channel = streams[1].channel
        close = channel.close
        def close_counted():
            with lock:
                was_closed = channel.closed
                close()
                if counters is not None and not was_closed and channel.closed:
                    counters["open"] -= 1
        channel.close = close_counted
        return streams

    return exec_command


def _fake_ssh_client(**exec_kwargs) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.connect.return_value = None
    client.exec_command.side_effect = _exec_ok(**exec_kwargs)
    client.get_transport.return_value.is_active.return_value = True
    return client


def _tofu_config(known_hosts: str) -> SSHConfig:
    return SSHConfig(
        enabled=True,
        host=HOST,
        user="jbodmap",
        key_path="/run/ssh/id_example",
        strict_host_key_checking=False,
        known_hosts_path=known_hosts,
    )


class HostKeyPolicyIsUnchangedTests(unittest.TestCase):
    """Reused and grouped connections are built by ``_client``, policy and all."""

    def _assert_tofu(self, client: MagicMock, known_hosts: str) -> None:
        client.load_host_keys.assert_called_once_with(known_hosts)
        policy = client.set_missing_host_key_policy.call_args.args[0]
        self.assertIsInstance(policy, AutoPinHostKeyPolicy)
        self.assertEqual(policy.known_hosts_path, known_hosts)
        self.assertNotIsInstance(policy, paramiko.AutoAddPolicy)
        self.assertNotIsInstance(policy, paramiko.WarningPolicy)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_session_pins_through_known_hosts_and_connects_once(self, ssh_client_cls: MagicMock) -> None:
        client = _fake_ssh_client()
        ssh_client_cls.return_value = client
        known_hosts = "/var/lib/example/known_hosts"
        with patch.object(SSHProbe, "_prepare_known_hosts_path", return_value=known_hosts):
            session = SSHProbe(_tofu_config(known_hosts)).open_session()
            first = session.run_command("sudo -n midclt call core.get_jobs")
            second = session.run_command("sudo -n midclt call core.get_jobs")
            session.close()

        self.assertTrue(first.ok and second.ok)
        client.connect.assert_called_once()
        self.assertEqual(session.connections, 1)
        self._assert_tofu(client, known_hosts)
        client.close.assert_called_once()

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_session_rejects_unknown_keys_in_strict_mode(self, ssh_client_cls: MagicMock) -> None:
        client = _fake_ssh_client()
        ssh_client_cls.return_value = client
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = SSHProbe(
                SSHConfig(
                    enabled=True,
                    host=HOST,
                    user="jbodmap",
                    key_path="/run/ssh/id_example",
                    known_hosts_path=f"{temp_dir}/missing-known-hosts",
                )
            )
            session = probe.open_session()
            session.run_command("true")
        self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_session_rechecks_the_host_key_when_it_reconnects(self, ssh_client_cls: MagicMock) -> None:
        dropped = _fake_ssh_client()
        dropped.get_transport.return_value.is_active.return_value = False
        fresh = _fake_ssh_client()
        ssh_client_cls.side_effect = [dropped, fresh]
        known_hosts = "/var/lib/example/known_hosts"
        with patch.object(SSHProbe, "_prepare_known_hosts_path", return_value=known_hosts):
            session = SSHProbe(_tofu_config(known_hosts)).open_session()
            session.run_command("first")
            session.run_command("second")

        self.assertEqual(session.connections, 2)
        dropped.close.assert_called_once()
        # The replacement connection went through the same policy setup.
        self._assert_tofu(dropped, known_hosts)
        self._assert_tofu(fresh, known_hosts)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_session_reports_a_rejected_host_key_as_a_failed_command(self, ssh_client_cls: MagicMock) -> None:
        client = _fake_ssh_client()
        client.connect.side_effect = paramiko.SSHException("Server not found in known_hosts")
        ssh_client_cls.return_value = client
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = SSHProbe(
                SSHConfig(enabled=True, host=HOST, user="jbodmap", known_hosts_path=f"{temp_dir}/kh")
            )
            result = probe.open_session().run_command("true")
        self.assertFalse(result.ok)
        self.assertIn("known_hosts", result.stderr)
        client.exec_command.assert_not_called()

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_planned_groups_share_one_pinned_connection_and_keep_group_order(self, ssh_client_cls: MagicMock) -> None:
        counters = {"open": 0, "peak": 0}
        client = _fake_ssh_client(delay=0.01, counters=counters)
        ssh_client_cls.return_value = client
        known_hosts = "/var/lib/example/known_hosts"
        groups = []
        for bay in range(12):
            def planner(results, bay=bay):
                return [f"smartctl -x /dev/da{bay}"] if len(results) == 1 else []
            groups.append((planner, [f"smartctl -j -a /dev/da{bay}"]))
        with patch.object(SSHProbe, "_prepare_known_hosts_path", return_value=known_hosts):
            outcomes = SSHProbe(_tofu_config(known_hosts))._run_planned_command_groups_sync(groups)

        client.connect.assert_called_once()
        self._assert_tofu(client, known_hosts)
        self.assertEqual(
            [[result.command for result in outcome] for outcome in outcomes],
            [[f"smartctl -j -a /dev/da{bay}", f"smartctl -x /dev/da{bay}"] for bay in range(12)],
        )
        self.assertLessEqual(counters["peak"], MAX_PARALLEL_CHANNELS_PER_CONNECTION)
        self.assertGreater(counters["peak"], 1)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_planned_groups_settle_on_a_lower_server_session_limit(self, ssh_client_cls: MagicMock) -> None:
        """A server with MaxSessions below 8 refuses extra channels; those commands wait and retry."""
        server_limit = 3
        lock = threading.Lock()
        state = {"open": 0, "peak": 0, "refused": 0}
        ok_exec = _exec_ok(delay=0.01)

        def exec_command(command: str, timeout=None):
            with lock:
                if state["open"] >= server_limit:
                    state["refused"] += 1
                    raise paramiko.ChannelException(1, "Administratively prohibited")
                state["open"] += 1
                state["peak"] = max(state["peak"], state["open"])
            streams = ok_exec(command, timeout)
            channel = streams[1].channel
            close = channel.close
            def close_counted():
                with lock:
                    was_closed = channel.closed
                    close()
                    if not was_closed and channel.closed:
                        state["open"] -= 1
            channel.close = close_counted
            return streams

        client = _fake_ssh_client()
        client.exec_command.side_effect = exec_command
        ssh_client_cls.return_value = client
        known_hosts = "/var/lib/example/known_hosts"
        groups = [((lambda _results: []), [f"smartctl -j -a /dev/da{bay}"]) for bay in range(12)]
        with patch.object(SSHProbe, "_prepare_known_hosts_path", return_value=known_hosts):
            outcomes = SSHProbe(_tofu_config(known_hosts))._run_planned_command_groups_sync(groups)

        self.assertTrue(all(result.ok for outcome in outcomes for result in outcome))
        self.assertEqual(sum(len(outcome) for outcome in outcomes), 12)
        self.assertGreater(state["refused"], 0)
        self.assertLessEqual(state["peak"], server_limit)
        client.connect.assert_called_once()

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_a_refused_only_channel_fails_its_command_without_looping(self, ssh_client_cls: MagicMock) -> None:
        client = _fake_ssh_client()
        client.exec_command.side_effect = paramiko.ChannelException(1, "Administratively prohibited")
        ssh_client_cls.return_value = client
        known_hosts = "/var/lib/example/known_hosts"
        with patch.object(SSHProbe, "_prepare_known_hosts_path", return_value=known_hosts):
            outcomes = SSHProbe(_tofu_config(known_hosts))._run_planned_command_groups_sync(
                [((lambda _results: []), ["a"])]
            )
        self.assertEqual([[result.ok for result in outcome] for outcome in outcomes], [[False]])
        self.assertEqual(client.exec_command.call_count, 1)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_planned_groups_fail_every_group_when_the_host_key_is_rejected(self, ssh_client_cls: MagicMock) -> None:
        client = _fake_ssh_client()
        client.connect.side_effect = paramiko.SSHException("Server not found in known_hosts")
        ssh_client_cls.return_value = client
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = SSHProbe(SSHConfig(enabled=True, host=HOST, user="jbodmap", known_hosts_path=f"{temp_dir}/kh"))
            outcomes = probe._run_planned_command_groups_sync(
                [(lambda _results: [], ["a"]), (lambda _results: [], ["b"])]
            )
        self.assertEqual([[result.ok for result in outcome] for outcome in outcomes], [[False], [False]])
        client.exec_command.assert_not_called()


def _service(temp_dir: str, *, ssh_probe=None, settings: Settings | None = None, platform: str = "scale") -> InventoryService:
    settings = settings or Settings()
    system = SystemConfig(
        id="ssh-reuse",
        truenas=TrueNASConfig(platform=platform),
        ssh=SSHConfig(enabled=True, host=HOST, user="jbodmap", commands=[], extra_hosts=[OTHER_HOST]),
    )
    probe = ssh_probe if ssh_probe is not None else SSHProbe(system.ssh)
    return InventoryService(
        settings,
        system,
        AsyncMock(),
        probe,
        None,
        MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
    )


def _ok(commands) -> list[SSHCommandResult]:
    return [SSHCommandResult(command=command, ok=True, stdout="ok", exit_code=0) for command in commands]


class PerHostPlanCoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.service = _service(self._temp.name)
        self.probe: SSHProbe = self.service.ssh_probe  # type: ignore[assignment]

    async def test_plans_queued_behind_a_round_share_the_next_connection(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()
        single_calls: list[list[str]] = []
        group_calls: list[int] = []

        async def run_planned(_planner, *, initial_commands=None):
            single_calls.append(list(initial_commands or []))
            started.set()
            await release.wait()
            return _ok(initial_commands or [])

        async def run_groups(groups, *, max_parallel_channels):
            group_calls.append(len(groups))
            self.assertLessEqual(max_parallel_channels, MAX_PARALLEL_CHANNELS_PER_CONNECTION)
            return [_ok(commands) for _planner, commands in groups]

        self.probe.run_planned_commands = AsyncMock(side_effect=run_planned)  # type: ignore[method-assign]
        self.probe.run_planned_command_groups = AsyncMock(side_effect=run_groups)  # type: ignore[method-assign]

        first = asyncio.create_task(
            self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["bay-0"], host=HOST)
        )
        await started.wait()
        rest = [
            asyncio.create_task(
                self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=[f"bay-{bay}"], host=HOST)
            )
            for bay in range(1, 12)
        ]
        for _ in range(10):
            await asyncio.sleep(0)
        # Nothing else touches the host while the first round holds its lock.
        self.assertEqual(group_calls, [])
        release.set()
        results = await asyncio.gather(first, *rest)

        self.assertEqual(single_calls, [["bay-0"]])
        self.assertEqual(group_calls, [11])
        self.assertEqual([[item.command for item in result] for result in results], [[f"bay-{bay}"] for bay in range(12)])

    async def test_other_ssh_work_on_the_host_waits_for_the_whole_round(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()
        batch_started = asyncio.Event()

        async def run_planned(_planner, *, initial_commands=None):
            started.set()
            await release.wait()
            return _ok(initial_commands or [])

        async def run_commands(commands):
            batch_started.set()
            return _ok(commands)

        self.probe.run_planned_commands = AsyncMock(side_effect=run_planned)  # type: ignore[method-assign]
        self.probe.run_commands = AsyncMock(side_effect=run_commands)  # type: ignore[method-assign]
        planned = asyncio.create_task(
            self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["bay-0"], host=HOST)
        )
        await started.wait()
        self.assertTrue(self.service._ssh_session_lock_for_host(HOST).locked())
        batch = asyncio.create_task(self.service._run_ssh_commands(["led"], HOST))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(batch_started.is_set())
        release.set()
        await asyncio.gather(planned, batch)
        self.assertTrue(batch_started.is_set())
        await self._drained()

    async def test_the_host_lock_is_released_between_rounds(self) -> None:
        """An LED batch waiting on the host runs before the next SMART round, not after the grid."""
        release_first = asyncio.Event()
        started = asyncio.Event()
        order: list[str] = []

        async def run_planned(_planner, *, initial_commands=None):
            order.append("round-1")
            started.set()
            await release_first.wait()
            return _ok(initial_commands or [])

        async def run_groups(groups, *, max_parallel_channels):
            order.append("round-2")
            return [_ok(commands) for _planner, commands in groups]

        async def run_commands(commands):
            order.append("led")
            return _ok(commands)

        self.probe.run_planned_commands = AsyncMock(side_effect=run_planned)  # type: ignore[method-assign]
        self.probe.run_planned_command_groups = AsyncMock(side_effect=run_groups)  # type: ignore[method-assign]
        self.probe.run_commands = AsyncMock(side_effect=run_commands)  # type: ignore[method-assign]
        first = asyncio.create_task(
            self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["bay-0"], host=HOST)
        )
        await started.wait()
        led = asyncio.create_task(self.service._run_ssh_commands(["led"], HOST))
        for _ in range(5):
            await asyncio.sleep(0)
        more = [
            asyncio.create_task(
                self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=[f"bay-{bay}"], host=HOST)
            )
            for bay in (1, 2)
        ]
        for _ in range(5):
            await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first, led, *more)
        self.assertEqual(order, ["round-1", "led", "round-2"])
        await self._drained()

    async def test_plans_for_different_hosts_never_share_a_round(self) -> None:
        hosts_run: list[str] = []

        async def fake_planned(self_probe, _planner, *, initial_commands=None):
            hosts_run.append(self_probe.config.host)
            return _ok(initial_commands or [])

        with patch.object(SSHProbe, "run_planned_commands", fake_planned):
            await asyncio.gather(
                self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["a"], host=HOST),
                self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["b"], host=OTHER_HOST),
            )
        self.assertEqual(sorted(hosts_run), [HOST, OTHER_HOST])

    async def test_a_failed_round_fails_only_its_own_plans_and_the_queue_keeps_draining(self) -> None:
        calls = 0

        async def run_planned(_planner, *, initial_commands=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic transport failure")
            return _ok(initial_commands or [])

        self.probe.run_planned_commands = AsyncMock(side_effect=run_planned)  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["a"], host=HOST)
        result = await self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["b"], host=HOST)
        self.assertEqual([item.command for item in result], ["b"])
        await self._drained()

    async def _drained(self) -> None:
        for drain in list(self.service._ssh_plan_drains.values()):
            await drain
        self.assertEqual(self.service._ssh_plan_drains, {})
        self.assertFalse(self.service._ssh_session_lock_for_host(HOST).locked())

    async def test_a_waiter_that_clears_its_traceback_does_not_strand_the_queue(self) -> None:
        """unittest's assertRaises clears frames; that once closed the drain coroutine."""
        import traceback

        calls = 0

        async def run_planned(_planner, *, initial_commands=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic transport failure")
            return _ok(initial_commands or [])

        self.probe.run_planned_commands = AsyncMock(side_effect=run_planned)  # type: ignore[method-assign]
        try:
            await self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["a"], host=HOST)
        except RuntimeError as exc:
            traceback.clear_frames(exc.__traceback__)
        result = await asyncio.wait_for(
            self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["b"], host=HOST), 5,
        )
        self.assertEqual([item.command for item in result], ["b"])
        await self._drained()

    async def test_cancelling_the_drain_cancels_queued_plans_instead_of_stranding_them(self) -> None:
        started = asyncio.Event()

        async def run_planned(_planner, *, initial_commands=None):
            started.set()
            await asyncio.Event().wait()

        self.probe.run_planned_commands = AsyncMock(side_effect=run_planned)  # type: ignore[method-assign]
        first = asyncio.create_task(
            self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["a"], host=HOST)
        )
        await started.wait()
        queued = asyncio.create_task(
            self.service._run_ssh_planned_commands(lambda _r: [], initial_commands=["b"], host=HOST)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        drain = self.service._ssh_plan_drains[HOST]
        drain.cancel()
        results = await asyncio.wait_for(asyncio.gather(first, queued, return_exceptions=True), 5)
        self.assertTrue(all(isinstance(item, asyncio.CancelledError) for item in results))
        self.assertFalse(self.service._ssh_session_lock_for_host(HOST).locked())


class OwnedSSHWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = _service(self.temp.name)
        self.probe = self.service.ssh_probe
        self.loop_errors = []
        self.loop = asyncio.get_running_loop()
        self.old_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda _loop, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        for _ in range(10):
            await asyncio.sleep(0)
        self.loop.set_exception_handler(self.old_handler)
        self.assertEqual(self.loop_errors, [])

    async def wait_until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.001)

    async def exercise_cancel(self, kind, *, late_error=False):
        from tests.test_ssh_probe import MemorySSHWire
        opened = threading.Event()
        worker_cleanup = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        issued = []
        planners = []
        late_failure = threading.Event()
        wire = MemorySSHWire(lambda _channel, _command: opened.set())
        original = self.probe._run_single_command

        def command(*args, **kwargs):
            issued.append(args[1])
            try:
                return original(*args, **kwargs)
            finally:
                worker_cleanup.set()
                if not release.wait(3):
                    raise AssertionError("test worker cleanup was not released")
                finished.set()

        def planner(results):
            planners.append(len(results))
            return ["planned-after-cancel"]

        if kind == "direct":
            call = self.service._run_ssh_commands(["first", "queued"], HOST)
        elif kind == "single":
            call = self.service._run_ssh_command("first", HOST)
        elif kind == "plan":
            call = self.service._run_ssh_planned_commands(planner, initial_commands=["first", "queued"], host=HOST)
        else:
            call = self.probe.run_planned_command_groups([
                (planner, ["first", "queued"]), (planner, ["other", "other-queued"]),
            ], max_parallel_channels=1)

        original_batch = self.probe._run_commands_sync
        def batch(*args, **kwargs):
            try:
                return original_batch(*args, **kwargs)
            finally:
                if late_error:
                    late_failure.set()
                    raise RuntimeError("synthetic late worker failure")

        with patch.object(self.probe, "_client", return_value=wire.client), patch.object(self.probe, "_run_single_command", side_effect=command), patch.object(self.probe, "_run_commands_sync", side_effect=batch):
            task = asyncio.create_task(call)
            contender = None
            try:
                await self.wait_until(opened.is_set)
                owner = self.service._ssh_plan_drains[HOST] if kind == "plan" else task
                owner.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(owner.done(), "cancel released ownership before worker teardown")
                if kind != "groups":
                    self.assertTrue(self.service._ssh_session_lock_for_host(HOST).locked())
                    contender = asyncio.create_task(self.service._ssh_session_lock_for_host(HOST).acquire())
                await self.wait_until(worker_cleanup.is_set)
                self.assertTrue(wire.closed.is_set(), "cancel did not interrupt active transport")
                owner.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(owner.done(), "repeated cancel abandoned worker drain")
                if contender:
                    self.assertFalse(contender.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(finished.is_set())
                self.assertEqual(issued, ["first"])
                self.assertEqual(planners, [])
                self.assertEqual(late_failure.is_set(), late_error)
                self.assertTrue(all(channel.closed for channel in wire.channels))
                if contender:
                    await asyncio.wait_for(contender, 1)
                    self.service._ssh_session_lock_for_host(HOST).release()
            finally:
                release.set()
                wire.close()
                await asyncio.gather(task, return_exceptions=True)
                for drain in list(self.service._ssh_plan_drains.values()):
                    drain.cancel()
                    await asyncio.gather(drain, return_exceptions=True)
                if contender:
                    if not contender.done():
                        contender.cancel()
                    await asyncio.gather(contender, return_exceptions=True)
                    lock = self.service._ssh_session_lock_for_host(HOST)
                    if lock.locked():
                        lock.release()
                await self.wait_until(finished.is_set)

    async def test_direct_batch_cancel_retains_host_lock_and_stops_queued_command(self):
        await self.exercise_cancel("direct")

    async def test_single_command_cancel_retains_host_lock(self):
        await self.exercise_cancel("single")

    async def test_plan_drain_shutdown_cancel_retains_host_lock(self):
        await self.exercise_cancel("plan")

    async def test_group_cancel_stops_queued_groups_and_planner_batches(self):
        await self.exercise_cancel("groups")

    async def test_repeated_cancel_observes_late_worker_failure(self):
        await self.exercise_cancel("direct", late_error=True)

    async def test_coalesced_parallel_drain_retains_both_workers_through_cancel(self):
        from tests.test_ssh_probe import MemorySSHWire
        release = threading.Event()
        cleanup = set()
        finished = set()
        issued = []
        original = self.probe._run_single_command
        wire = MemorySSHWire(lambda _channel, _command: None)
        def command(*args, **kwargs):
            name = args[1]
            issued.append(name)
            try:
                return original(*args, **kwargs)
            finally:
                cleanup.add(name)
                if not release.wait(3):
                    raise AssertionError("parallel cleanup was not released")
                finished.add(name)
        with patch.object(self.probe, "_client", return_value=wire.client), patch.object(self.probe, "_run_single_command", side_effect=command):
            tasks = [asyncio.create_task(self.service._run_ssh_planned_commands(
                lambda _results: ["later-planner"], initial_commands=[name, name + "-queued"], host=HOST,
            )) for name in ("first", "other")]
            queued = None
            try:
                await self.wait_until(lambda: len(wire.commands) == 2)
                queued = asyncio.create_task(self.service._run_ssh_planned_commands(
                    lambda _results: [], initial_commands=["next-round"], host=HOST,
                ))
                await self.wait_until(lambda: bool(self.service._ssh_plan_queues[HOST]))
                drain = self.service._ssh_plan_drains[HOST]
                drain.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(drain.done(), "parallel drain abandoned its workers")
                await self.wait_until(lambda: len(cleanup) == 2)
                drain.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(drain.done())
                self.assertTrue(self.service._ssh_session_lock_for_host(HOST).locked())
                self.assertFalse(queued.done())
                self.assertTrue(all(channel.closed for channel in wire.channels))
                release.set()
                outcomes = await asyncio.gather(*tasks, queued, return_exceptions=True)
                self.assertTrue(all(isinstance(item, asyncio.CancelledError) for item in outcomes))
                self.assertEqual(set(issued), {"first", "other"})
                self.assertEqual(finished, {"first", "other"})
                self.assertEqual(self.service._ssh_plan_drains, {})
                self.assertFalse(self.service._ssh_session_lock_for_host(HOST).locked())
            finally:
                release.set()
                wire.close()
                await asyncio.gather(*tasks, *([queued] if queued else []), return_exceptions=True)
                await asyncio.gather(*list(self.service._ssh_plan_drains.values()), return_exceptions=True)

    async def test_cancel_during_initial_planner_prevents_connection_and_commands(self):
        started = threading.Event()
        release = threading.Event()
        done = threading.Event()
        def planner(_results):
            started.set()
            try:
                if not release.wait(3):
                    raise AssertionError("planner was not released")
                return ["not-authorized-after-cancel"]
            finally:
                done.set()
        with patch.object(self.probe, "_client") as client:
            task = asyncio.create_task(self.probe.run_planned_commands(planner, initial_commands=[]))
            try:
                await self.wait_until(started.is_set)
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(done.is_set())
                client.assert_not_called()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_interrupts_exec_wait_with_saturated_default_executor(self):
        from concurrent.futures import ThreadPoolExecutor
        from tests.test_ssh_probe import MemorySSHWire
        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        wire = MemorySSHWire(lambda _channel, _command: None)
        wire.acknowledge = False
        with patch.object(self.probe, "_client", return_value=wire.client):
            task = asyncio.create_task(self.service._run_ssh_commands(["first", "queued"], HOST))
            try:
                await self.wait_until(lambda: bool(wire.commands))
                task.cancel()
                try:
                    async with asyncio.timeout(0.4):
                        while not wire.closed.is_set():
                            await asyncio.sleep(0.001)
                except TimeoutError:
                    self.fail("cancellation interrupt queued behind its blocked SSH worker")
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(wire.commands, ["first"])
                self.assertFalse(self.service._ssh_session_lock_for_host(HOST).locked())
            finally:
                wire.close()
                await asyncio.gather(task, return_exceptions=True)

    async def test_ordinary_plan_waiter_cancel_does_not_stop_shared_worker(self):
        from tests.test_ssh_probe import MemorySSHWire
        wire = MemorySSHWire(lambda _channel, _command: None)
        with patch.object(self.probe, "_client", return_value=wire.client):
            task = asyncio.create_task(self.service._run_ssh_planned_commands(
                lambda _results: [], initial_commands=["first"], host=HOST,
            ))
            try:
                await self.wait_until(lambda: bool(wire.commands))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(wire.closed.is_set())
                self.assertTrue(self.service._ssh_session_lock_for_host(HOST).locked())
                wire.feed(wire.channels[0], b"ok", eof=True, status=0)
                await asyncio.gather(*list(self.service._ssh_plan_drains.values()))
                self.assertTrue(wire.closed.is_set())
                self.assertFalse(self.service._ssh_session_lock_for_host(HOST).locked())
            finally:
                wire.close()
                await asyncio.gather(task, *list(self.service._ssh_plan_drains.values()), return_exceptions=True)


class SSHRunnerShutdownTests(unittest.TestCase):
    def test_runner_shutdown_drains_real_workers_and_observes_late_errors(self):
        from tests.test_ssh_probe import MemorySSHWire
        for planned in (False, True):
            with self.subTest(planned=planned), tempfile.TemporaryDirectory() as temp_dir:
                service = _service(temp_dir)
                probe = service.ssh_probe
                # Also bounds exact-predecessor replay, whose worker ignores
                # runner cancellation and reaches only its channel read timeout.
                probe.config = probe.config.model_copy(update={"timeout_seconds": 0.2})
                wire = MemorySSHWire(lambda _channel, _command: None)
                observations = []
                loop_errors = []
                completed = threading.Event()
                issued = []
                loop_holder = []
                original = probe._run_single_command
                def command(*args, **kwargs):
                    issued.append(args[1])
                    try:
                        return original(*args, **kwargs)
                    finally:
                        observed = threading.Event()
                        def observe():
                            observations.append(service._ssh_session_lock_for_host(HOST).locked())
                            # A second shutdown cancellation must not release it.
                            owner = service._ssh_plan_drains.get(HOST) if planned else task_holder[0]
                            if owner is not None:
                                owner.cancel()
                            observed.set()
                        loop_holder[0].call_soon_threadsafe(observe)
                        if not observed.wait(2):
                            raise AssertionError("shutdown loop stopped before worker cleanup")
                        completed.set()
                        raise RuntimeError("synthetic shutdown worker error")
                task_holder = []
                async def main():
                    loop = asyncio.get_running_loop()
                    loop_holder.append(loop)
                    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
                    if planned:
                        call = service._run_ssh_planned_commands(
                            lambda _r: ["later"], initial_commands=["first", "queued"], host=HOST,
                        )
                    else:
                        call = service._run_ssh_commands(["first", "queued"], HOST)
                    task_holder.append(asyncio.create_task(call))
                    async with asyncio.timeout(2):
                        while not wire.commands:
                            await asyncio.sleep(0.001)
                    # Returning enters asyncio.Runner's real all-task cancellation.
                with patch.object(probe, "_client", return_value=wire.client), patch.object(probe, "_run_single_command", side_effect=command):
                    try:
                        asyncio.run(main())
                    finally:
                        wire.close()
                self.assertEqual(observations, [True])
                self.assertTrue(completed.is_set())
                self.assertEqual(issued, ["first"])
                self.assertEqual(loop_errors, [])
                self.assertFalse(service._ssh_session_lock_for_host(HOST).locked())
                self.assertEqual(service._ssh_plan_drains, {})
                self.assertTrue(all(channel.closed for channel in wire.channels))


class InventoryOwnedSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = _service(self.temp.name)
        self.probe = self.service.ssh_probe
        self.loop = asyncio.get_running_loop()
        self.loop_errors = []
        self.old_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda _loop, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        for _ in range(10):
            await asyncio.sleep(0)
        self.loop.set_exception_handler(self.old_handler)
        self.assertEqual(self.loop_errors, [])

    async def wait_until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.001)

    async def exercise_session_cancel(self, *, acknowledge=True, late_worker=False, late_close=False):
        from app.services.inventory import _ssh_command_session
        from tests.test_ssh_probe import MemorySSHWire

        wire = MemorySSHWire(lambda _channel, _command: None)
        wire.acknowledge = acknowledge
        session = self.probe.open_session()
        worker_cleanup = threading.Event()
        release_worker = threading.Event()
        worker_done = threading.Event()
        close_started = threading.Event()
        release_close = threading.Event()
        close_done = threading.Event()
        original_command = self.probe._run_single_command
        original_close = session.close
        late_failures = []

        def command(*args, **kwargs):
            try:
                return original_command(*args, **kwargs)
            finally:
                worker_cleanup.set()
                if not release_worker.wait(3):
                    raise AssertionError("session worker was not released")
                worker_done.set()
                if late_worker:
                    late_failures.append("worker")
                    raise RuntimeError("synthetic session worker failure")

        def close():
            close_started.set()
            try:
                if not release_close.wait(3):
                    raise AssertionError("session close was not released")
                original_close()
                if late_close:
                    late_failures.append("close")
                    raise RuntimeError("synthetic session close failure")
            finally:
                close_done.set()

        async def operation():
            token = _ssh_command_session.set(session)
            try:
                await self.service._run_ssh_command("first", HOST)
                await self.service._run_ssh_command("not-after-cancel", HOST)
            finally:
                _ssh_command_session.reset(token)

        with patch.object(self.probe, "_client", return_value=wire.client), patch.object(
            self.probe, "_run_single_command", side_effect=command,
        ), patch.object(session, "close", side_effect=close):
            task = asyncio.create_task(operation())
            contender = None
            try:
                await self.wait_until(lambda: bool(wire.commands))
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done(), "session cancellation abandoned the running worker")
                lock = self.service._ssh_session_lock_for_host(HOST)
                self.assertTrue(lock.locked())
                contender = asyncio.create_task(lock.acquire())
                await self.wait_until(worker_cleanup.is_set)
                self.assertTrue(wire.closed.is_set(), "session cancellation did not interrupt Paramiko")
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(contender.done())
                release_worker.set()
                await self.wait_until(close_started.is_set)
                self.assertTrue(worker_done.is_set())
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done(), "repeated cancellation abandoned session close")
                self.assertFalse(contender.done(), "host lock released before session close drained")
                release_close.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(close_done.is_set())
                self.assertEqual(wire.commands, ["first"])
                self.assertEqual(late_failures, (["worker"] if late_worker else []) + (["close"] if late_close else []))
                self.assertTrue(all(channel.closed for channel in wire.channels))
                await asyncio.wait_for(contender, 1)
                lock.release()
            finally:
                release_worker.set()
                release_close.set()
                wire.close()
                await asyncio.gather(task, return_exceptions=True)
                if contender is not None:
                    if not contender.done():
                        contender.cancel()
                    await asyncio.gather(contender, return_exceptions=True)
                    if self.service._ssh_session_lock_for_host(HOST).locked():
                        self.service._ssh_session_lock_for_host(HOST).release()
                # Drain the old implementation's abandoned default-executor work
                # before the patch and private fixture go out of scope on RED.
                await self.wait_until(worker_done.is_set)
                original_close()

    async def test_session_output_cancel_retains_host_lock_through_worker_and_close(self):
        await self.exercise_session_cancel()

    async def test_session_exec_ack_cancel_interrupts_real_paramiko_and_drains(self):
        from concurrent.futures import ThreadPoolExecutor

        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        await self.exercise_session_cancel(acknowledge=False)

    async def test_session_connect_cancel_interrupts_registered_client_before_admission(self):
        from app.services.inventory import _ssh_command_session
        from tests.test_ssh_probe import MemorySSHWire

        wire = MemorySSHWire()
        connected = threading.Event()
        finished = threading.Event()
        session = self.probe.open_session()

        def connect(**kwargs):
            self.assertEqual(kwargs["hostname"], HOST)
            connected.set()
            try:
                if not wire.closed.wait(3):
                    raise AssertionError("connecting client was not interrupted")
                raise EOFError("synthetic interrupted handshake")
            finally:
                finished.set()

        async def operation():
            token = _ssh_command_session.set(session)
            try:
                await self.service._run_ssh_command("not-after-connect-cancel", HOST)
            finally:
                _ssh_command_session.reset(token)

        with patch("app.services.ssh_probe.paramiko.SSHClient", return_value=wire.client), patch.object(
            wire.client, "connect", side_effect=connect,
        ):
            task = asyncio.create_task(operation())
            try:
                await self.wait_until(connected.is_set)
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done() and not finished.is_set(), "connect owner released before handshake stopped")
                await self.wait_until(wire.closed.is_set)
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(finished.is_set())
                self.assertEqual(wire.commands, [])
                self.assertFalse(self.service._ssh_session_lock_for_host(HOST).locked())
            finally:
                wire.close()
                await asyncio.gather(task, return_exceptions=True)
                await self.wait_until(finished.is_set)
                session.close()

    async def test_session_cancel_before_executor_admission_starts_no_connection(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.services.inventory import _ssh_command_session

        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupied = threading.Event()
        release = threading.Event()
        def occupy():
            occupied.set()
            if not release.wait(3):
                raise AssertionError("occupied executor was not released")
        blocker = self.loop.run_in_executor(None, occupy)
        session = self.probe.open_session()

        async def operation():
            token = _ssh_command_session.set(session)
            try:
                await self.service._run_ssh_command("not-after-queued-cancel", HOST)
            finally:
                _ssh_command_session.reset(token)

        with patch.object(self.probe, "_client", return_value=_fake_ssh_client()) as new_client:
            task = asyncio.create_task(operation())
            try:
                await self.wait_until(occupied.is_set)
                await self.wait_until(self.service._ssh_session_lock_for_host(HOST).locked)
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done(), "queued executor ownership was abandoned")
                self.assertTrue(self.service._ssh_session_lock_for_host(HOST).locked())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                new_client.assert_not_called()
                self.assertFalse(self.service._ssh_session_lock_for_host(HOST).locked())
            finally:
                release.set()
                await asyncio.gather(task, blocker, return_exceptions=True)
                session.close()

    async def test_successful_disk_sync_reuses_real_paramiko_session_between_polls(self):
        from tests.test_ssh_probe import MemorySSHWire

        answers = iter([b"268071\n", b'[{"id": 268071, "state": "RUNNING"}]', b'[{"id": 268071, "state": "SUCCESS"}]'])
        held = []
        between = []
        def respond(channel, _command):
            observed = threading.Event()
            def on_loop():
                held.append(self.service._ssh_session_lock_for_host(None).locked())
                observed.set()
            self.loop.call_soon_threadsafe(on_loop)
            if not observed.wait(2):
                raise AssertionError("poll command lost event loop")
            wire.feed(channel, next(answers), eof=True, status=0)
        wire = MemorySSHWire(respond)
        async def pause(_seconds):
            between.append(self.service._ssh_session_lock_for_host(None).locked())
        self.service._disk_inventory_sync_sleep = pause
        with patch.object(self.probe, "_client", return_value=wire.client) as new_client:
            try:
                result = await self.service.sync_disk_inventory(DiskInventorySyncMode.full)
                self.assertEqual(result.state, "SUCCESS")
                self.assertEqual(new_client.call_count, 1)
                self.assertEqual(held, [True, True, True])
                self.assertEqual(between, [False])
                self.assertEqual(len(wire.commands), 3)
                self.assertTrue(wire.closed.is_set())
                self.assertTrue(all(channel.closed for channel in wire.channels))
                self.assertIsNone(self.service._disk_inventory_sync_active_job_id)
            finally:
                wire.close()

    async def test_session_cancel_observes_late_worker_and_close_exceptions(self):
        await self.exercise_session_cancel(late_worker=True, late_close=True)

    async def test_disk_sync_poll_cancel_stops_followups_and_retains_both_locks(self):
        from tests.test_ssh_probe import MemorySSHWire

        cleanup_started = threading.Event()
        release = threading.Event()
        cleanup_done = threading.Event()
        session = self.probe.open_session()
        original_close = session.close

        def respond(channel, command):
            if "disk.sync_all" in command:
                wire.feed(channel, b"268071\n", eof=True, status=0)

        wire = MemorySSHWire(respond)

        def close():
            cleanup_started.set()
            try:
                if not release.wait(3):
                    raise AssertionError("disk sync cleanup was not released")
                original_close()
            finally:
                cleanup_done.set()

        with patch.object(self.probe, "_client", return_value=wire.client), patch.object(
            self.probe, "open_session", return_value=session,
        ), patch.object(session, "close", side_effect=close):
            task = asyncio.create_task(self.service.sync_disk_inventory(DiskInventorySyncMode.full))
            try:
                await self.wait_until(lambda: len(wire.commands) == 2)
                task.cancel()
                await self.wait_until(cleanup_started.is_set)
                self.assertTrue(self.service._ssh_session_lock_for_host(None).locked(), "poll released host before cleanup")
                self.assertTrue(self.service._disk_inventory_sync_lock.locked())
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(cleanup_done.is_set())
                self.assertEqual(len(wire.commands), 2)
                self.assertEqual(self.service._disk_inventory_sync_active_job_id, 268071)
                self.assertFalse(self.service._ssh_session_lock_for_host(None).locked())
                self.assertFalse(self.service._disk_inventory_sync_lock.locked())
            finally:
                release.set()
                wire.close()
                await asyncio.gather(task, return_exceptions=True)
                await self.wait_until(cleanup_done.is_set)
                original_close()

    async def test_cancel_during_successful_disk_sync_close_retains_sync_owner(self):
        from tests.test_ssh_probe import MemorySSHWire

        answers = iter([b"268071\n", b'[{"id": 268071, "state": "SUCCESS"}]'])
        wire = MemorySSHWire(lambda channel, _command: wire.feed(channel, next(answers), eof=True, status=0))
        session = self.probe.open_session()
        close_started = threading.Event()
        release = threading.Event()
        close_done = threading.Event()
        original_close = session.close

        def close():
            close_started.set()
            try:
                if not release.wait(3):
                    raise AssertionError("successful sync close was not released")
                original_close()
                raise RuntimeError("synthetic late sync close failure")
            finally:
                close_done.set()

        with patch.object(self.probe, "_client", return_value=wire.client), patch.object(
            self.probe, "open_session", return_value=session,
        ), patch.object(session, "close", side_effect=close):
            task = asyncio.create_task(self.service.sync_disk_inventory(DiskInventorySyncMode.full))
            try:
                await self.wait_until(close_started.is_set)
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done(), "disk sync abandoned its close worker")
                self.assertTrue(self.service._disk_inventory_sync_lock.locked())
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(close_done.is_set())
                self.assertEqual(len(wire.commands), 2)
                self.assertTrue(wire.closed.is_set())
                self.assertFalse(self.service._disk_inventory_sync_lock.locked())
            finally:
                release.set()
                wire.close()
                await asyncio.gather(task, return_exceptions=True)
                await self.wait_until(close_done.is_set)


class InventorySessionRunnerShutdownTests(unittest.TestCase):
    def test_runner_shutdown_retains_session_worker_close_and_late_errors(self):
        from tests.test_ssh_probe import MemorySSHWire

        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            probe = service.ssh_probe
            probe.config = probe.config.model_copy(update={"timeout_seconds": 0.2})
            wire = MemorySSHWire(lambda _channel, _command: None)
            session = probe.open_session()
            observations = []
            loop_errors = []
            task_holder = []
            loop_holder = []
            original_command = probe._run_single_command
            original_close = session.close

            def observe(phase):
                observed = threading.Event()
                def on_loop():
                    observations.append((phase, service._ssh_session_lock_for_host(None).locked(), service._disk_inventory_sync_lock.locked()))
                    task_holder[0].cancel()
                    observed.set()
                loop_holder[0].call_soon_threadsafe(on_loop)
                if not observed.wait(2):
                    raise AssertionError("runner stopped before session teardown")

            def command(*args, **kwargs):
                try:
                    return original_command(*args, **kwargs)
                finally:
                    observe("worker")
                    raise RuntimeError("synthetic shutdown session worker error")

            def close():
                if session._client is not None:
                    observe("close")
                original_close()

            async def main():
                loop = asyncio.get_running_loop()
                loop_holder.append(loop)
                loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
                task_holder.append(asyncio.create_task(service.sync_disk_inventory(DiskInventorySyncMode.full)))
                async with asyncio.timeout(2):
                    while not wire.commands:
                        await asyncio.sleep(0.001)

            with patch.object(probe, "_client", return_value=wire.client), patch.object(
                probe, "open_session", return_value=session,
            ), patch.object(probe, "_run_single_command", side_effect=command), patch.object(session, "close", side_effect=close):
                try:
                    asyncio.run(main())
                finally:
                    wire.close()
            self.assertEqual(observations, [("worker", True, True), ("close", True, True)])
            self.assertEqual(len(wire.commands), 1)
            self.assertEqual(loop_errors, [])
            self.assertTrue(all(channel.closed for channel in wire.channels))
            self.assertFalse(service._ssh_session_lock_for_host(None).locked())
            self.assertFalse(service._disk_inventory_sync_lock.locked())


class DiskSyncPollReusesOneConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_sync_polls_over_one_connection_and_locks_per_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(app=AppConfig(disk_inventory_sync_timeout_seconds=60))
            service = _service(temp_dir, settings=settings)
            probe: SSHProbe = service.ssh_probe  # type: ignore[assignment]
            answers = iter(["268071\n"] + ['[{"id": 268071, "state": "RUNNING"}]'] * 4 + ['[{"id": 268071, "state": "SUCCESS"}]'])
            lock_held: list[bool] = []
            loop = asyncio.get_running_loop()

            def exec_command(command: str, timeout=None):
                lock_held.append(
                    asyncio.run_coroutine_threadsafe(self._locked(service), loop).result(2)
                )
                return _memory_streams(next(answers).encode())

            clients: list[MagicMock] = []

            def new_client(*, _cancel=None):
                client = _fake_ssh_client()
                client.exec_command.side_effect = exec_command
                clients.append(client)
                return client

            probe._client = new_client  # type: ignore[method-assign]
            lock_between_polls: list[bool] = []

            async def fake_sleep(_seconds: float) -> None:
                lock_between_polls.append(service._ssh_session_lock_for_host(None).locked())

            service._disk_inventory_sync_sleep = fake_sleep
            result = await service.sync_disk_inventory(DiskInventorySyncMode.full)

        self.assertEqual(result.state, "SUCCESS")
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].exec_command.call_count, 6)
        clients[0].close.assert_called_once()
        # The host lock is held for each command and released between polls, so
        # LED actions can still run while a sync waits.
        self.assertEqual(lock_held, [True] * 6)
        self.assertTrue(lock_between_polls)
        self.assertFalse(any(lock_between_polls))

    @staticmethod
    async def _locked(service: InventoryService) -> bool:
        return service._ssh_session_lock_for_host(None).locked()


class SmallRepeatTests(unittest.TestCase):
    """#448 item 7: one alias read, one status haystack, one identifier set per slot."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)

    def test_scale_enclosure_options_read_aliases_once_per_build(self) -> None:
        service = _service(self._temp.name, platform="scale")
        alias_store = MagicMock()
        alias_store.list_aliases.return_value = []
        service.sas_fabric_alias_store = alias_store
        service._build_scale_linux_enclosure_options(ParsedSSHData())
        self.assertEqual(alias_store.list_aliases.call_count, 1)
        alias_store.list_aliases.reset_mock()
        options = service._build_enclosure_options(
            MagicMock(enclosures=[{"id": "enc-a", "name": "Shelf", "label": "Shelf"}]),
            ParsedSSHData(),
            {"id": None},
        )
        self.assertEqual([option.id for option in options], ["enc-a"])
        self.assertEqual(alias_store.list_aliases.call_count, 1)

    def test_unfinalized_options_keep_raw_labels_for_the_caller(self) -> None:
        service = _service(self._temp.name, platform="scale")
        alias_store = MagicMock()
        alias_store.list_aliases.return_value = []
        service.sas_fabric_alias_store = alias_store
        with patch.object(
            InventoryService,
            "_ses_enclosure_to_option",
            return_value=EnclosureOption(id="enc-z", label="Front 24", slot_count=24),
        ):
            options = service._build_core_ssh_enclosure_options(
                ParsedSSHData(ses_enclosures=[MagicMock()]), filter_value=None, excluded_ids=set(), finalize=False,
            )
        self.assertEqual([option.label for option in options], ["Front 24"])
        alias_store.list_aliases.assert_not_called()

    def test_status_haystack_answers_like_status_contains(self) -> None:
        cases = [
            ({"status": "OK"}, ("ok",)),
            ({"value": "Fault sensed"}, ("fault",)),
            ({"status": "default"}, ("fault",)),
            ({"status": "Not installed"}, ("installed",)),
            ({"status": {"nested": "fault"}}, ("fault",)),
            ({}, ("ok",)),
        ]
        for raw_status, needles in cases:
            with self.subTest(raw_status=raw_status):
                self.assertEqual(
                    InventoryService._haystack_contains(InventoryService._status_haystack(raw_status), *needles),
                    InventoryService._status_contains(raw_status, *needles),
                )

    def test_slot_detail_entry_derives_identifiers_once(self) -> None:
        service = _service(self._temp.name)
        slot = SlotView(
            slot=3, slot_label="03", row_index=0, column_index=3, enclosure_id="enc-a",
            device_name="sdc", serial="SN-EXAMPLE-3", present=True,
        )
        with patch.object(
            InventoryService, "_slot_detail_identifiers", autospec=True,
            side_effect=InventoryService._slot_detail_identifiers,
        ) as identifiers:
            service._build_slot_detail_entry(slot, smart_summary=None)
        self.assertEqual(identifiers.call_count, 1)

    def test_smart_cache_store_uses_the_callers_key(self) -> None:
        service = _service(self._temp.name)
        slot = SlotView(slot=1, slot_label="01", row_index=0, column_index=1, device_name="sdb", serial="SN-EX-1", present=True)
        key = service._smart_cache_key(slot)
        from app.models.domain import SmartSummaryView

        with patch.object(InventoryService, "_smart_cache_key", side_effect=AssertionError("recomputed")):
            stored = service._store_smart_summary_cache(
                slot, SmartSummaryView(available=True, temperature_c=30), cache_key=key,
            )
        self.assertTrue(stored)
        self.assertIn(key, service._smart_cache)


class SmartCacheEvictionGateTests(unittest.TestCase):
    def test_eviction_still_sees_an_expiry_edited_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            key = ("ssh-reuse", "scale", "enc-a", 1, ("sdb",), ("serial", "sn-1"))
            from app.models.domain import SmartSummaryView

            service._smart_cache[key] = SmartSummaryView(available=True)
            service._smart_cache_until[key] = utcnow() + timedelta(hours=1)
            service._evict_expired_smart_cache_entries()
            self.assertIn(key, service._smart_cache)
            service._smart_cache_until[key] = utcnow() - timedelta(days=365)
            service._evict_expired_smart_cache_entries()
            self.assertNotIn(key, service._smart_cache)

    def test_an_entry_without_an_expiry_is_still_evicted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            from app.models.domain import SmartSummaryView

            fresh = ("ssh-reuse", "scale", "enc-a", 1, ("sdb",), ("serial", "sn-1"))
            orphan = ("ssh-reuse", "scale", "enc-a", 2, ("sdc",), ("serial", "sn-2"))
            service._smart_cache[fresh] = SmartSummaryView(available=True)
            service._smart_cache_until[fresh] = utcnow() + timedelta(hours=1)
            service._smart_cache[orphan] = SmartSummaryView(available=True)
            service._evict_expired_smart_cache_entries()
            self.assertIn(fresh, service._smart_cache)
            self.assertNotIn(orphan, service._smart_cache)


if __name__ == "__main__":
    unittest.main()
