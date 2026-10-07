"""One SSH connection per refresh: a probe keeps its last clean connection briefly.

A manual refresh runs several SSH calls against one host a few seconds apart.
Each used to open its own connection, so one refresh could trip sshd
MaxStartups or an IPS "SSH scan" rule (five connections in two minutes). A probe
built with ``idle_seconds`` now hands its last cleanly finished connection to
the next call. Every connection still comes from ``SSHProbe._client``, with the
same host-key policy.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import paramiko

from tests.test_ssh_session_reuse import HOST, OTHER_HOST, _fake_ssh_client, _service

from app.config import SSHConfig
from app.services import ssh_probe as ssh_probe_module
from app.services.ssh_probe import SSH_CONNECTION_IDLE_SECONDS, SSHProbe


def _config(host: str = HOST) -> SSHConfig:
    return SSHConfig(enabled=True, host=host, user="jbodmap", key_path="/run/ssh/id_example")


def _clients(ssh_client_cls: MagicMock) -> list[MagicMock]:
    made: list[MagicMock] = []

    def make() -> MagicMock:
        client = _fake_ssh_client()
        made.append(client)
        return client

    ssh_client_cls.side_effect = make
    return made


@patch("app.services.ssh_probe.paramiko.SSHClient")
class KeptConnectionTests(unittest.TestCase):
    def test_every_entry_point_shares_one_kept_connection(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)

        probe._run_commands_sync(["a", "b"])
        probe._run_command_sync("c")
        probe._run_planned_commands_sync(lambda results: ["e"] if len(results) == 1 else [], ["d"])
        probe._run_planned_command_groups_sync([(lambda _r: [], ["f"]), (lambda _r: [], ["g"])])

        self.assertEqual(len(clients), 1)
        clients[0].connect.assert_called_once()
        self.assertEqual(clients[0].exec_command.call_count, 7)
        clients[0].close.assert_not_called()
        probe.close_idle()
        clients[0].close.assert_called_once()

    def test_without_idle_seconds_each_call_opens_and_closes_its_own(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config())

        probe._run_commands_sync(["a"])
        probe._run_command_sync("b")
        probe._run_planned_commands_sync(lambda _r: [], ["c"])

        self.assertEqual(len(clients), 3)
        for client in clients:
            client.close.assert_called_once()

    def test_reuse_is_logged_as_no_new_connection(self, ssh_client_cls: MagicMock) -> None:
        _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)
        with self.assertLogs("app.services.ssh_probe", level="INFO") as logs:
            probe._run_commands_sync(["a"])
            probe._run_planned_commands_sync(lambda _r: [], ["b"])
        self.assertIn("connections=1 commands=1", logs.output[0])
        self.assertIn("connections=0 plans=1", logs.output[1])

    def test_an_unused_connection_closes_after_the_idle_time(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=0.05)

        probe._run_commands_sync(["a"])
        deadline = time.monotonic() + 2
        while not clients[0].close.called and time.monotonic() < deadline:
            time.sleep(0.01)

        clients[0].close.assert_called_once()
        probe._run_commands_sync(["b"])
        self.assertEqual(len(clients), 2)
        probe.close_idle()

    def test_a_dropped_kept_connection_is_replaced_through_the_same_policy(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)

        probe._run_commands_sync(["a"])
        clients[0].get_transport.return_value.is_active.return_value = False
        probe._run_commands_sync(["b"])

        self.assertEqual(len(clients), 2)
        clients[0].close.assert_called_once()
        for client in clients:
            client.connect.assert_called_once()
            self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)

    def test_a_connection_whose_command_cleanup_escaped_is_not_kept(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)

        with patch.object(SSHProbe, "_run_single_command", side_effect=RuntimeError("cleanup not confirmed")):
            probe._run_planned_commands_sync(lambda _r: [], ["a"])
            probe._run_commands_sync(["b"])
        probe._run_commands_sync(["c"])

        self.assertEqual(len(clients), 3)
        clients[0].close.assert_called_once()
        clients[1].close.assert_called_once()

    def test_a_failed_connect_keeps_nothing(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)
        ssh_client_cls.side_effect = None
        refused = _fake_ssh_client()
        refused.connect.side_effect = paramiko.SSHException("Server not found in known_hosts")
        ssh_client_cls.return_value = refused

        result = probe._run_command_sync("a")

        self.assertFalse(result.ok)
        self.assertIsNone(probe._idle)
        self.assertEqual(clients, [])

    def test_an_old_connection_is_retired_instead_of_kept(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)

        with patch.object(ssh_probe_module, "SSH_CONNECTION_MAX_AGE_SECONDS", 0):
            probe._run_commands_sync(["a"])
        probe._run_commands_sync(["b"])

        self.assertEqual(len(clients), 2)
        clients[0].close.assert_called_once()

    def test_a_kept_connection_that_aged_out_while_idle_is_not_handed_out(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)

        probe._run_commands_sync(["a"])
        self.assertIsNotNone(probe._idle)
        # It was young when kept; the age limit passes before the next call.
        with patch.object(ssh_probe_module, "SSH_CONNECTION_MAX_AGE_SECONDS", 0):
            probe._run_planned_commands_sync(lambda _r: [], ["b"])

        self.assertEqual(len(clients), 2)
        clients[0].close.assert_called_once()
        self.assertEqual(clients[0].exec_command.call_count, 1)
        self.assertEqual(clients[1].exec_command.call_args.args[0], "b")

    def test_overlapping_calls_never_share_one_connection(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)
        probe._run_commands_sync(["warm"])
        entered, release = threading.Event(), threading.Event()
        run = clients[0].exec_command.side_effect

        def slow(command: str, timeout=None):
            entered.set()
            release.wait(5)
            return run(command, timeout)

        clients[0].exec_command.side_effect = slow
        busy = threading.Thread(target=probe._run_commands_sync, args=(["slow"],))
        busy.start()
        self.assertTrue(entered.wait(5))
        probe._run_commands_sync(["fast"])
        release.set()
        busy.join(5)

        # The busy connection was not handed out; the overlapping call opened
        # its own and kept it, so the busy one closed when it finished.
        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[1].exec_command.call_args.args[0], "fast")
        clients[0].close.assert_called_once()
        clients[1].close.assert_not_called()

    def test_a_cancelled_call_does_not_keep_its_connection(self, ssh_client_cls: MagicMock) -> None:
        clients = _clients(ssh_client_cls)
        probe = SSHProbe(_config(), idle_seconds=60)
        self.addCleanup(probe.close_idle)
        cancellation = ssh_probe_module._WorkerCancellation()

        with probe._owned_client(cancellation) as (client, _reused):
            cancellation.cancelled.set()

        self.assertIsNone(probe._idle)
        clients[0].close.assert_called()


class InventoryKeepsOneProbePerHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)

    async def test_other_hosts_get_one_kept_probe_each(self) -> None:
        service = _service(self._temp.name, ssh_probe=SSHProbe(_config(), idle_seconds=SSH_CONNECTION_IDLE_SECONDS))

        other = service._ssh_probe_for_host(OTHER_HOST)

        self.assertIs(service._ssh_probe_for_host(HOST), service.ssh_probe)
        self.assertIs(service._ssh_probe_for_host(None), service.ssh_probe)
        self.assertIs(service._ssh_probe_for_host(OTHER_HOST), other)
        self.assertEqual(other.config.host, OTHER_HOST)
        self.assertEqual(other.idle_seconds, SSH_CONNECTION_IDLE_SECONDS)

    async def test_refreshes_share_one_connection_per_host(self) -> None:
        clients: list[MagicMock] = []
        with patch("app.services.ssh_probe.paramiko.SSHClient") as ssh_client_cls:
            ssh_client_cls.side_effect = lambda: clients.append(_fake_ssh_client()) or clients[-1]
            probe = SSHProbe(_config(), idle_seconds=SSH_CONNECTION_IDLE_SECONDS)
            service = _service(self._temp.name, ssh_probe=probe)
            self.addCleanup(lambda: [p.close_idle() for p in (probe, *service._ssh_host_probes.values())])
            service.system.ssh.commands = ["lsblk -J"]

            # A manual refresh in the order the browser sends it: the inventory
            # read, then SMART for the bays, then an SES read.
            for _refresh in range(2):
                await service._collect_inventory_source_bundle()
                await service._run_ssh_planned_commands(lambda _r: [], initial_commands=["smartctl -j -a /dev/sda"])
                await service._run_ssh_commands(["sg_ses --page=2 /dev/sg0"])

        by_host: dict[str, list[MagicMock]] = {}
        for client in clients:
            by_host.setdefault(client.connect.call_args.kwargs["hostname"], []).append(client)
        # Two refreshes, each with an inventory plan, a SMART round and an SES
        # read on the system host plus SES discovery on its extra host: one
        # login per host in all.
        self.assertEqual({host: len(made) for host, made in by_host.items()}, {HOST: 1, OTHER_HOST: 1})
        self.assertGreater(by_host[HOST][0].exec_command.call_count, 6)


if __name__ == "__main__":
    unittest.main()
