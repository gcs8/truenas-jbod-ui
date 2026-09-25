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
        try:
            if delay:
                time.sleep(delay)
        finally:
            if counters is not None:
                with lock:
                    counters["open"] -= 1
        stdin, stdout, stderr = MagicMock(), MagicMock(), MagicMock()
        stdout.read.return_value = f"{command} output".encode()
        stderr.read.return_value = b""
        stdout.channel.recv_exit_status.return_value = 0
        return stdin, stdout, stderr

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
                stdin, stdout, stderr = MagicMock(), MagicMock(), MagicMock()
                stdout.read.return_value = next(answers).encode()
                stderr.read.return_value = b""
                stdout.channel.recv_exit_status.return_value = 0
                return stdin, stdout, stderr

            clients: list[MagicMock] = []

            def new_client():
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
