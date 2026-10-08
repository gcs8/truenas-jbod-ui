from __future__ import annotations

import asyncio
import gc
import os
import threading
import weakref
import time
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import paramiko
from paramiko.client import SSHClient as RealSSHClient

from app.config import ENV_OVERRIDES, SSHConfig, get_settings
from app.services import ssh_probe
from app.services.ssh_probe import AutoPinHostKeyPolicy, SSHCommandResult, SSHProbe, redact_ssh_command


def _memory_streams(output=b"ok", error=b""):
    wire = MemorySSHWire(lambda channel, _command: MemorySSHWire.feed(
        channel, output, error, eof=True, status=0,
    ))
    _stdin, stdout, stderr = wire.client.exec_command("synthetic")
    # Existing input tests assert exact writes without copying synthetic secrets
    # into channel internals; deadline/output tests use real stdin too.
    return MagicMock(), stdout, stderr


class SSHProbeTests(unittest.TestCase):
    def test_tofu_pinning_serializes_reload_add_save_for_shared_known_hosts(self) -> None:
        shared_hosts: set[str] = set()
        active_saves = 0
        peak_saves = 0
        state_lock = threading.Lock()
        start = threading.Barrier(2)
        errors: list[BaseException] = []

        class FakeHostKeys:
            def __init__(self) -> None:
                self.hosts: set[str] = set()

            def add(self, hostname: str, _key_type: str, _key) -> None:
                self.hosts.add(hostname)

        class FakeClient:
            def __init__(self) -> None:
                self._host_keys = FakeHostKeys()

            def load_host_keys(self, _path: str) -> None:
                self._host_keys.hosts = set(shared_hosts)

            def get_host_keys(self) -> FakeHostKeys:
                return self._host_keys

            def save_host_keys(self, _path: str) -> None:
                nonlocal active_saves, peak_saves
                with state_lock:
                    active_saves += 1
                    peak_saves = max(peak_saves, active_saves)
                time.sleep(0.02)
                shared_hosts.clear()
                shared_hosts.update(self._host_keys.hosts)
                with state_lock:
                    active_saves -= 1

        def pin(hostname: str) -> None:
            try:
                client = FakeClient()
                key = MagicMock()
                key.get_name.return_value = "ssh-rsa"
                start.wait()
                AutoPinHostKeyPolicy("/tmp/shared-known-hosts").missing_host_key(
                    cast(paramiko.SSHClient, client),
                    hostname,
                    key,
                )
            except BaseException as exc:  # noqa: BLE001 - surface thread failures below.
                errors.append(exc)

        threads = [
            threading.Thread(target=pin, args=("host-a.example.test",)),
            threading.Thread(target=pin, args=("host-b.example.test",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(shared_hosts, {"host-a.example.test", "host-b.example.test"})
        self.assertEqual(peak_saves, 1)

    def test_redact_ssh_command_masks_inline_quantastor_server_secret(self) -> None:
        command = "/usr/bin/qs disk-list --json '--server=localhost,jbodmap,super-secret'"

        redacted = redact_ssh_command(command)

        self.assertIn("--server=localhost,jbodmap,***", redacted)
        self.assertNotIn("super-secret", redacted)

    def test_redact_ssh_command_masks_common_secret_arguments(self) -> None:
        command = "tool --password secret-pass token=abc123 --api-key=key-value"

        redacted = redact_ssh_command(command)

        self.assertNotIn("secret-pass", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("key-value", redacted)
        self.assertIn("--password", redacted)
        self.assertIn("token=***", redacted)
        self.assertIn("--api-key=***", redacted)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_client_rejects_unknown_host_keys_by_default(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(SSHProbe, "_prepare_known_hosts_path") as prepare_known_hosts_path:
                probe = SSHProbe(
                    SSHConfig(
                        enabled=True,
                        host="archive-core.example.test",
                        user="jbodmap",
                        key_path="/run/ssh/id_truenas",
                        known_hosts_path=f"{temp_dir}/missing-known-hosts",
                    )
                )

                probe._client()

        prepare_known_hosts_path.assert_not_called()
        ssh_client.load_host_keys.assert_not_called()
        policy = ssh_client.set_missing_host_key_policy.call_args.args[0]
        self.assertIsInstance(policy, paramiko.RejectPolicy)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_client_uses_password_auth_when_configured(self, ssh_client_cls: MagicMock) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None

        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="unvr.gcs8.io",
                user="root",
                key_path="",
                password="secret-pass",
                strict_host_key_checking=False,
            )
        )

        client = probe._client()

        self.assertIs(client, ssh_client)
        ssh_client.connect.assert_called_once_with(
            hostname="unvr.gcs8.io",
            port=22,
            username="root",
            key_filename=None,
            password="secret-pass",
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_client_uses_tofu_pinning_when_strict_host_key_checking_disabled(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None
        default_known_hosts_path = SSHConfig().known_hosts_path

        with patch.object(SSHProbe, "_prepare_known_hosts_path", return_value=default_known_hosts_path) as prepare_known_hosts_path:
            probe = SSHProbe(
                SSHConfig(
                    enabled=True,
                    host="unvr.gcs8.io",
                    user="root",
                    key_path="",
                    password="secret-pass",
                    strict_host_key_checking=False,
                )
            )

            probe._client()

        prepare_known_hosts_path.assert_called_once_with(default_known_hosts_path)
        ssh_client.load_host_keys.assert_called_once_with(default_known_hosts_path)
        policy = ssh_client.set_missing_host_key_policy.call_args.args[0]
        self.assertIsInstance(policy, AutoPinHostKeyPolicy)
        self.assertEqual(policy.known_hosts_path, default_known_hosts_path)
        self.assertNotIsInstance(policy, paramiko.WarningPolicy)
        self.assertNotIsInstance(policy, paramiko.AutoAddPolicy)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_client_rejects_unknown_keys_when_strict_mode_has_no_known_hosts_path(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None

        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                known_hosts_path=None,
                strict_host_key_checking=True,
            )
        )

        probe._client()

        policy = ssh_client.set_missing_host_key_policy.call_args.args[0]
        self.assertIsInstance(policy, paramiko.RejectPolicy)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_client_keeps_key_auth_when_key_path_present(self, ssh_client_cls: MagicMock) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None

        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                key_path="/run/ssh/id_truenas",
                password="",
                strict_host_key_checking=False,
            )
        )

        probe._client()

        ssh_client.connect.assert_called_once_with(
            hostname="archive-core.example.test",
            port=22,
            username="jbodmap",
            key_filename="/run/ssh/id_truenas",
            password=None,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_client_falls_back_to_keyboard_interactive_when_password_auth_is_rejected(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        transport = MagicMock()
        transport.is_authenticated.return_value = True
        ssh_client.get_transport.return_value = transport
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.side_effect = paramiko.BadAuthenticationType(
            "bad auth type",
            ["publickey", "keyboard-interactive"],
        )

        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="192.168.1.174",
                user="root",
                key_path="",
                password="secret-pass",
                strict_host_key_checking=False,
            )
        )

        client = probe._client()

        self.assertIs(client, ssh_client)
        ssh_client.connect.assert_called_once()
        transport.auth_interactive.assert_called_once()
        handler = transport.auth_interactive.call_args.args[1]
        self.assertEqual(
            handler("", "", [("Password: ", False)]),
            ["secret-pass"],
        )

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_command_sync_returns_failure_result_when_connection_setup_fails(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.side_effect = TimeoutError("timed out")

        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="192.168.1.174",
                user="root",
                password="secret-pass",
                strict_host_key_checking=False,
            )
        )

        result = probe._run_command_sync("sudo -n smartctl -x -j /dev/sda")

        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 255)
        self.assertIn("timed out", result.stderr)
        ssh_client.close.assert_called_once_with()

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_command_sync_can_extend_only_the_remote_command_timeout(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.exec_command.return_value = _memory_streams(b"null\n")
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                timeout_seconds=15,
                strict_host_key_checking=False,
            )
        )

        result = probe._run_command_sync(
            "sudo -n /usr/local/bin/midclt call disk.multipath_sync",
            timeout_seconds=180,
        )

        self.assertTrue(result.ok)
        self.assertEqual(ssh_client.connect.call_args.kwargs["timeout"], 15)
        self.assertEqual(ssh_client.connect.call_args.kwargs["banner_timeout"], 15)
        self.assertEqual(ssh_client.connect.call_args.kwargs["auth_timeout"], 15)
        ssh_client.exec_command.assert_called_once_with(
            "sudo -n /usr/local/bin/midclt call disk.multipath_sync",
            timeout=180,
        )

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_commands_sync_returns_failure_results_for_each_command_when_connection_setup_fails(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.side_effect = TimeoutError("timed out")

        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="192.168.1.174",
                user="root",
                password="secret-pass",
                strict_host_key_checking=False,
                commands=["lsblk -OJ", "sudo -n smartctl -x -j /dev/sda"],
            )
        )

        results = probe._run_commands_sync()

        self.assertEqual(len(results), 2)
        self.assertTrue(all(not item.ok for item in results))
        self.assertTrue(all(item.exit_code == 255 for item in results))
        self.assertTrue(all("timed out" in item.stderr for item in results))

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_commands_accepts_explicit_command_batch_on_one_connection(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None

        def exec_command(command: str, timeout: int):
            stdin, stdout, stderr = _memory_streams(f"{command} output".encode())
            return stdin, stdout, stderr

        ssh_client.exec_command.side_effect = exec_command
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                commands=["configured command"],
                strict_host_key_checking=False,
            )
        )

        results = probe._run_commands_sync(["dynamic one", "dynamic two"])

        self.assertEqual([item.command for item in results], ["dynamic one", "dynamic two"])
        self.assertTrue(all(item.ok for item in results))
        ssh_client.connect.assert_called_once()
        self.assertEqual(ssh_client.exec_command.call_count, 2)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_commands_writes_shared_stdin_data_for_each_command(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None
        streams: list[MagicMock] = []

        def exec_command(command: str, timeout: int):
            stdin, stdout, stderr = _memory_streams(b"ok")
            streams.append(stdin)
            return stdin, stdout, stderr

        ssh_client.exec_command.side_effect = exec_command
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="synthetic.example.test",
                user="operator",
                strict_host_key_checking=False,
            )
        )
        stdin_data = "c3ludGhldGljLXNlcnZlci1zcGVj\n"

        results = probe._run_commands_sync(["first command", "second command"], stdin_data=stdin_data)

        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(len(streams), 2)
        for stream in streams:
            stream.write.assert_called_once_with(stdin_data)
            stream.flush.assert_called_once_with()
            stream.channel.shutdown_write.assert_called_once_with()
            stream.close.assert_called_once_with()
        for call in ssh_client.exec_command.call_args_list:
            self.assertNotIn(stdin_data.strip(), call.args[0])

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_commands_rejects_stdin_data_with_sudo_password(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="synthetic.example.test",
                user="operator",
                sudo_password="synthetic-sudo-password",
                strict_host_key_checking=False,
            )
        )

        results = probe._run_commands_sync(["sudo -n /usr/bin/id"], stdin_data="synthetic-input\n")

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].exit_code, 255)
        self.assertIn("cannot be combined", results[0].stderr)
        self.assertNotIn("synthetic-input", results[0].stderr)
        self.assertNotIn("synthetic-sudo-password", results[0].stderr)
        ssh_client.exec_command.assert_not_called()

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_commands_sync_preserves_completed_results_when_batch_aborts(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                strict_host_key_checking=False,
            )
        )

        with patch.object(
            probe,
            "_run_single_command",
            side_effect=[
                SSHCommandResult(command="first", ok=True, stdout="first output", exit_code=0),
                RuntimeError("transport reset"),
            ],
        ):
            results = probe._run_commands_sync(["first", "second"])

        self.assertEqual([item.command for item in results], ["first", "second"])
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].stdout, "first output")
        self.assertFalse(results[1].ok)
        self.assertIn("transport reset", results[1].stderr)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_commands_logs_connection_count_for_batch(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None

        def exec_command(command: str, timeout: int):
            stdin, stdout, stderr = _memory_streams(b"ok")
            return stdin, stdout, stderr

        ssh_client.exec_command.side_effect = exec_command
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                strict_host_key_checking=False,
            )
        )

        with self.assertLogs("app.services.ssh_probe", level="INFO") as logs:
            probe._run_commands_sync(["uptime"])

        self.assertIn("connections=1", "\n".join(logs.output))

    def test_single_command_rejects_oversized_stdout_with_a_bounded_read(self) -> None:
        client = MagicMock()
        stdin, stdout, stderr = _memory_streams(b"x" * (ssh_probe.MAX_SSH_OUTPUT_BYTES + 1))
        client.exec_command.return_value = stdin, stdout, stderr
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="synthetic.example.test",
                user="operator",
                strict_host_key_checking=False,
            )
        )

        result = probe._run_single_command(client, "synthetic command")

        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, "")
        self.assertIn("output exceeded", result.stderr)
        self.assertTrue(stdout.channel.closed)
        self.assertTrue(stdout.closed)
        self.assertTrue(stderr.closed)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_planned_commands_reuses_one_connection_for_dynamic_batches(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None

        def exec_command(command: str, timeout: int):
            stdin, stdout, stderr = _memory_streams(f"{command} output".encode())
            return stdin, stdout, stderr

        ssh_client.exec_command.side_effect = exec_command
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                commands=["configured command"],
                strict_host_key_checking=False,
            )
        )

        def planner(results: list[SSHCommandResult]) -> list[str]:
            if len(results) == 1:
                return ["dynamic one", "dynamic two"]
            return []

        results = probe._run_planned_commands_sync(planner)

        self.assertEqual(
            [item.command for item in results],
            ["configured command", "dynamic one", "dynamic two"],
        )
        self.assertTrue(all(item.ok for item in results))
        ssh_client.connect.assert_called_once()
        self.assertEqual(ssh_client.exec_command.call_count, 3)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_planned_commands_preserves_completed_results_when_session_aborts(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client.__enter__.return_value = ssh_client
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.return_value = None
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="archive-core.example.test",
                user="jbodmap",
                strict_host_key_checking=False,
            )
        )

        with patch.object(
            probe,
            "_run_single_command",
            side_effect=[
                SSHCommandResult(command="seed", ok=True, stdout="seed output", exit_code=0),
                RuntimeError("session closed"),
            ],
        ):
            results = probe._run_planned_commands_sync(
                lambda _results: [],
                initial_commands=["seed", "dynamic"],
            )

        self.assertEqual([item.command for item in results], ["seed", "dynamic"])
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].stdout, "seed output")
        self.assertFalse(results[1].ok)
        self.assertIn("session closed", results[1].stderr)

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_run_planned_commands_returns_failure_for_planned_commands_when_connection_setup_fails(
        self,
        ssh_client_cls: MagicMock,
    ) -> None:
        ssh_client = MagicMock()
        ssh_client_cls.return_value = ssh_client
        ssh_client.connect.side_effect = TimeoutError("timed out")
        probe = SSHProbe(
            SSHConfig(
                enabled=True,
                host="192.168.1.174",
                user="root",
                password="secret-pass",
                strict_host_key_checking=False,
                commands=[],
            )
        )

        results = probe._run_planned_commands_sync(lambda _results: ["dynamic one", "dynamic two"])

        self.assertEqual(len(results), 2)
        self.assertEqual([item.command for item in results], ["dynamic one", "dynamic two"])
        self.assertTrue(all(not item.ok for item in results))
        self.assertTrue(all(item.exit_code == 255 for item in results))
        self.assertTrue(all("timed out" in item.stderr for item in results))
        ssh_client.connect.assert_called_once()


class MemorySSHWire:
    """Socket-free peer for the installed SSHClient/Channel framing and buffers."""

    server_object = None

    def __init__(self, respond=None):
        self.respond = respond or self.success
        self.channels = []
        self.commands = []
        self.before_open = None
        self.acknowledge = True
        self.closed = threading.Event()
        self.client = RealSSHClient()
        self.client._transport = self

    def get_log_channel(self):
        return "paramiko.synthetic"

    def _sanitize_packet_size(self, size):
        return size

    def is_active(self):
        return not self.closed.is_set()

    def get_exception(self):
        return None

    def open_session(self, timeout=None):
        if self.closed.is_set():
            raise EOFError("synthetic transport closed")
        if self.before_open:
            self.before_open()
        channel = paramiko.Channel(len(self.channels))
        channel._set_transport(self)
        channel._set_window(32768, 32768)
        channel._set_remote_channel(channel.chanid, 32768, 32768)
        self.channels.append(channel)
        return channel

    def _unlink_channel(self, _chanid):
        pass

    def _send_user_message(self, message):
        from paramiko.common import MSG_CHANNEL_REQUEST
        incoming = paramiko.Message(message.asbytes())
        kind = ord(incoming.get_byte())
        channel = self.channels[incoming.get_int()]
        if kind == MSG_CHANNEL_REQUEST and incoming.get_text() == "exec":
            incoming.get_boolean()
            command = incoming.get_text()
            self.commands.append(command)
            if self.acknowledge:
                channel._request_success(paramiko.Message())
            self.respond(channel, command)

    @staticmethod
    def feed(channel, output=b"", error=b"", *, eof=False, status=None):
        if output:
            channel._feed(output)
        if error:
            message = paramiko.Message()
            message.add_int(1)
            message.add_string(error)
            channel._feed_extended(paramiko.Message(message.asbytes()))
        if eof:
            channel._handle_eof(paramiko.Message())
        if status is not None:
            message = paramiko.Message()
            message.add_string("exit-status")
            message.add_boolean(False)
            message.add_int(status)
            channel._handle_request(paramiko.Message(message.asbytes()))

    def success(self, channel, command):
        self.feed(channel, (command + " output").encode(), eof=True, status=0)

    def close(self):
        self.closed.set()
        for channel in self.channels:
            channel.close()


class SSHCommandLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.probe = SSHProbe(SSHConfig(enabled=True, host="synthetic.example.test"))

    def run_bounded(self, wire, command="first", timeout=0.06):
        results = []
        worker = threading.Thread(target=lambda: results.append(
            self.probe._run_single_command(wire.client, command, timeout_seconds=timeout)
        ))
        worker.start()
        worker.join(0.8)
        completed_without_release = not worker.is_alive()
        closed_at_return = all(channel.closed for channel in wire.channels)
        # Rescue only failed baseline runs; never supply a status to make GREEN pass.
        wire.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(completed_without_release, "deadline required an external channel release")
        self.assertTrue(closed_at_return, "command returned with an open channel")
        return results[0]

    def test_eof_without_exit_status_times_out_without_external_release(self):
        wire = MemorySSHWire(lambda channel, _command: MemorySSHWire.feed(channel, eof=True))
        result = self.run_bounded(wire)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 255)
        self.assertIn("timed out", result.stderr.lower())

    def test_timeout_and_output_cap_close_before_session_reuse(self):
        for mode in ("timeout", "stdout-cap", "stderr-cap"):
            with self.subTest(mode=mode):
                def respond(channel, command):
                    if command == "next":
                        wire.success(channel, command)
                    elif mode == "timeout":
                        MemorySSHWire.feed(channel, eof=True)
                    else:
                        MemorySSHWire.feed(
                            channel, b"x" * 65 if mode == "stdout-cap" else b"",
                            b"x" * 65 if mode == "stderr-cap" else b"", eof=True, status=0,
                        )
                wire = MemorySSHWire(respond)
                session = self.probe.open_session()
                with patch.object(self.probe, "_client", return_value=wire.client), patch.object(ssh_probe, "MAX_SSH_OUTPUT_BYTES", 64):
                    results = []
                    worker = threading.Thread(target=lambda: results.extend([
                        session.run_command("first", timeout_seconds=0.05),
                        session.run_command("next", timeout_seconds=0.05),
                    ]))
                    prior_closed = []
                    wire.before_open = lambda: prior_closed.append(all(c.closed for c in wire.channels))
                    worker.start()
                    worker.join(0.8)
                    finished = not worker.is_alive()
                    closed = all(c.closed for c in wire.channels)
                    wire.close()
                    worker.join(1)
                    session.close()
                self.assertTrue(finished, "reused command needed external release")
                self.assertTrue(closed)
                self.assertEqual(prior_closed, [True, True])
                self.assertFalse(results[0].ok)
                self.assertTrue(results[1].ok)

    def test_stderr_window_is_drained_while_stdout_waits(self):
        threads = []
        errors = []
        def respond(channel, _command):
            def peer():
                try:
                    # Each bounded stderr window must drain before stdout/EOF arrive.
                    for _ in range(4):
                        MemorySSHWire.feed(channel, error=b"e" * 32768)
                        deadline = time.monotonic() + 0.5
                        while len(channel.in_stderr_buffer) and not channel.closed:
                            if time.monotonic() >= deadline:
                                raise AssertionError("stderr window was not drained")
                            time.sleep(0.001)
                    if not channel.closed:
                        MemorySSHWire.feed(channel, b"done", eof=True, status=0)
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=peer)
            threads.append(thread)
            thread.start()
        wire = MemorySSHWire(respond)
        try:
            result = self.run_bounded(wire, timeout=0.3)
        finally:
            wire.close()
            for thread in threads:
                thread.join(1)
        self.assertEqual(errors, [])
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout, "done")
        self.assertEqual(result.stderr, "e" * (4 * 32768))

    def test_exec_acknowledgement_wait_obeys_deadline(self):
        wire = MemorySSHWire(lambda _channel, _command: None)
        wire.acknowledge = False
        result = self.run_bounded(wire)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.stderr.lower())
        self.assertTrue(wire.closed.is_set())

    def test_combined_output_budget_exact_boundary_and_one_over(self):
        for error in (b"e" * 32, b"e" * 33):
            with self.subTest(error_bytes=len(error)), patch.object(ssh_probe, "MAX_SSH_OUTPUT_BYTES", 64):
                wire = MemorySSHWire(lambda channel, _command: MemorySSHWire.feed(
                    channel, b"o" * 32, error, eof=True, status=0,
                ))
                result = self.run_bounded(wire)
                self.assertEqual(result.ok, len(error) == 32)
                if result.ok:
                    self.assertEqual(result.stdout, "o" * 32)
                    self.assertEqual(result.stderr, "e" * 32)
                else:
                    self.assertEqual(result.stdout, "")
                    self.assertIn("output exceeded", result.stderr)

    def test_exit_status_before_final_output_does_not_truncate_output(self):
        workers = []
        def respond(channel, _command):
            MemorySSHWire.feed(channel, status=0)
            def peer():
                time.sleep(0.02)
                MemorySSHWire.feed(channel, b"late stdout", b"late stderr", eof=True)
            worker = threading.Thread(target=peer)
            workers.append(worker)
            worker.start()
        wire = MemorySSHWire(respond)
        try:
            result = self.run_bounded(wire, timeout=0.3)
        finally:
            wire.close()
            for worker in workers:
                worker.join(1)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "late stdout")
        self.assertEqual(result.stderr, "late stderr")

    def test_read_error_and_nonzero_exit_close_channel(self):
        for mode in ("read-error", "nonzero"):
            with self.subTest(mode=mode):
                def respond(channel, _command):
                    MemorySSHWire.feed(channel, b"data", b"failure", eof=True, status=7)
                    if mode == "read-error":
                        def fail(_size):
                            raise OSError("synthetic read failure")
                        channel.recv = fail
                wire = MemorySSHWire(respond)
                result = self.run_bounded(wire)
                self.assertFalse(result.ok)
                if mode == "nonzero":
                    self.assertEqual(result.exit_code, 7)
                    self.assertEqual(result.stdout, "data")
                    self.assertEqual(result.stderr, "failure")
                else:
                    self.assertIn("synthetic read failure", result.stderr)

    def test_a_failed_or_cancelled_command_frees_its_streams_without_the_cycle_collector(self):
        # Paramiko's BufferedFile.__del__ flushes even a closed file. If the cycle
        # collector finalizes its write buffer first, it prints "Exception ignored
        # in BufferedFile.__del__ ... I/O operation on closed file" (paramiko#2153).
        # The error that ends a command must not keep the streams in such a cycle.
        if gc.isenabled():
            gc.disable()
            self.addCleanup(gc.enable)
        for mode in ("read-error", "cancelled"):
            with self.subTest(mode=mode):
                cancel = ssh_probe._WorkerCancellation()

                def respond(channel, _command):
                    if mode == "cancelled":
                        cancel.cancelled.set()
                        return
                    def fail(_size):
                        raise OSError("synthetic read failure")
                    MemorySSHWire.feed(channel, b"data")
                    channel.recv = fail

                wire = MemorySSHWire(respond)
                streams = []
                real_exec = wire.client.exec_command

                def recording_exec(*args, **kwargs):
                    opened = real_exec(*args, **kwargs)
                    streams.extend(weakref.ref(stream) for stream in opened)
                    return opened

                wire.client.exec_command = recording_exec
                error = None
                try:
                    result = self.probe._run_single_command(wire.client, "first", timeout_seconds=5, _cancel=cancel)
                    self.assertFalse(result.ok)
                except asyncio.CancelledError as exc:
                    error = exc  # Held, as the executor future holds it.
                self.assertEqual(mode == "cancelled", error is not None)
                self.assertEqual(len(streams), 3)
                self.assertEqual([ref() for ref in streams], [None, None, None])

    def test_group_gate_does_not_release_a_channel_whose_close_failed(self):
        def respond(channel, command):
            MemorySSHWire.feed(channel, b"ok", eof=True, status=0)
            if command == "first":
                def failed_close():
                    raise OSError("synthetic channel close failure")
                channel.close = failed_close
        wire = MemorySSHWire(respond)
        try:
            with patch.object(self.probe, "_client", return_value=wire.client):
                with self.assertRaisesRegex(OSError, "synthetic channel close failure"):
                    self.probe._run_planned_command_groups_sync([
                        (lambda _r: [], ["first"]), (lambda _r: [], ["must-not-open"]),
                    ], max_parallel_channels=1)
            self.assertEqual(wire.commands, ["first"])
            self.assertFalse(wire.channels[0].closed)
        finally:
            for channel in wire.channels:
                channel.close = paramiko.Channel.close.__get__(channel)
            wire.close()

    def test_trickling_output_does_not_renew_absolute_deadline(self):
        stop = threading.Event()
        threads = []
        def respond(channel, _command):
            def peer():
                while not stop.wait(0.01) and not channel.closed:
                    MemorySSHWire.feed(channel, b"x")
            thread = threading.Thread(target=peer)
            threads.append(thread)
            thread.start()
        wire = MemorySSHWire(respond)
        try:
            result = self.run_bounded(wire)
            self.assertFalse(result.ok)
            self.assertIn("timed out", result.stderr.lower())
        finally:
            stop.set()
            for thread in threads:
                thread.join(1)


class KnownHostsPathSettingsTests(unittest.TestCase):
    """A configured known-hosts path is honoured; placeholders derive.

    Owner decision on #454 (supersedes the #318 fixed-path contract): an
    ``ssh.known_hosts_path`` set at the top level, per system, or through
    ``SSH_KNOWN_HOSTS_PATH`` is used as written, e.g. a host bind mount instead
    of the data volume. Unset, blank, the shipped default and the legacy
    ``/app/data/known_hosts`` container path still derive ``<data>/known_hosts``.
    """

    def _load_settings(self, config_lines: list[str], environment: dict[str, str]) -> tuple[Path, object]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config.yaml"
            config_path.write_text("\n".join(config_lines), encoding="utf-8")

            with patch.dict(
                "os.environ",
                {"APP_CONFIG_PATH": config_path.as_posix(), **environment},
                clear=False,
            ):
                if "SSH_KNOWN_HOSTS_PATH" not in environment:
                    os.environ.pop("SSH_KNOWN_HOSTS_PATH", None)
                get_settings.cache_clear()
                settings = get_settings()
                get_settings.cache_clear()
        return temp_root, settings

    def test_known_hosts_path_is_an_environment_override(self) -> None:
        self.assertEqual(ENV_OVERRIDES.get("SSH_KNOWN_HOSTS_PATH"), ("ssh", "known_hosts_path"))

    def test_unset_known_hosts_path_derives_from_the_runtime_layout(self) -> None:
        temp_root, settings = self._load_settings(
            ["systems:", "  - id: primary", "    ssh:", "      enabled: true"],
            {},
        )

        derived = temp_root / "known_hosts"
        self.assertEqual(Path(settings.ssh.known_hosts_path), derived)
        self.assertEqual([Path(system.ssh.known_hosts_path) for system in settings.systems], [derived])

    def test_placeholder_values_derive_from_the_runtime_layout(self) -> None:
        for placeholder in ("/app/data/known_hosts", '""'):
            with self.subTest(placeholder=placeholder):
                temp_root, settings = self._load_settings(
                    [
                        "ssh:",
                        f"  known_hosts_path: {placeholder}",
                        "systems:",
                        "  - id: primary",
                        "    ssh:",
                        f"      known_hosts_path: {placeholder}",
                    ],
                    {},
                )

                derived = temp_root / "known_hosts"
                self.assertEqual(Path(settings.ssh.known_hosts_path), derived)
                self.assertEqual([Path(system.ssh.known_hosts_path) for system in settings.systems], [derived])

    def test_configured_top_level_path_is_honoured_and_inherited_by_systems(self) -> None:
        temp_root, settings = self._load_settings(
            [
                "ssh:",
                "  known_hosts_path: /srv/operator/known_hosts",
                "systems:",
                "  - id: primary",
                "    ssh:",
                "      enabled: true",
                "  - id: legacy",
                "    ssh:",
                "      known_hosts_path: /app/data/known_hosts",
            ],
            {},
        )

        self.assertEqual(settings.ssh.known_hosts_path, "/srv/operator/known_hosts")
        self.assertEqual(
            [system.ssh.known_hosts_path for system in settings.systems],
            ["/srv/operator/known_hosts", "/srv/operator/known_hosts"],
        )
        self.assertNotEqual(Path(settings.ssh.known_hosts_path), temp_root / "known_hosts")

    def test_configured_per_system_path_is_honoured(self) -> None:
        temp_root, settings = self._load_settings(
            [
                "systems:",
                "  - id: primary",
                "    ssh:",
                "      enabled: true",
                "  - id: secondary",
                "    ssh:",
                "      known_hosts_path: /srv/secondary/known_hosts",
            ],
            {},
        )

        derived = str(temp_root / "known_hosts")
        self.assertEqual(settings.ssh.known_hosts_path, derived)
        self.assertEqual(
            [system.ssh.known_hosts_path for system in settings.systems],
            [derived, "/srv/secondary/known_hosts"],
        )

    def test_environment_variable_moves_the_known_hosts_file(self) -> None:
        # Also pins the _deep_merge copy: the env write must not leak into the
        # defaults it is compared against, or it would be read as a placeholder.
        _, settings = self._load_settings(
            ["systems:", "  - id: primary", "    ssh:", "      enabled: true"],
            {"SSH_KNOWN_HOSTS_PATH": "/srv/env/known_hosts"},
        )

        self.assertEqual(settings.ssh.known_hosts_path, "/srv/env/known_hosts")
        self.assertEqual(
            [system.ssh.known_hosts_path for system in settings.systems],
            ["/srv/env/known_hosts"],
        )

    def test_admin_ssh_targets_resolve_the_saved_systems_own_file(self) -> None:
        from app.config import known_hosts_path_for_target

        temp_root, settings = self._load_settings(
            [
                "ssh:",
                "  known_hosts_path: /srv/default/known_hosts",
                "systems:",
                "  - id: primary",
                "    ssh:",
                "      host: primary.example.test",
                "  - id: secondary",
                "    ssh:",
                "      host: secondary.example.test",
                "      extra_hosts: [peer.example.test]",
                "      known_hosts_path: /srv/secondary/known_hosts",
            ],
            {},
        )

        self.assertEqual(known_hosts_path_for_target(settings, system_id="secondary"), "/srv/secondary/known_hosts")
        self.assertEqual(known_hosts_path_for_target(settings, target_host="PEER.example.test"), "/srv/secondary/known_hosts")
        self.assertEqual(known_hosts_path_for_target(settings, system_id="primary"), "/srv/default/known_hosts")
        self.assertEqual(known_hosts_path_for_target(settings, target_host="new.example.test"), settings.ssh.known_hosts_path)

    def test_documentation_advertises_the_override(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in (".env.example", "docs/SSH_READ_ONLY_SETUP.md"):
            with self.subTest(document=relative):
                self.assertTrue(
                    "SSH_KNOWN_HOSTS_PATH" in (root / relative).read_text(encoding="utf-8"),
                    f"{relative} must document SSH_KNOWN_HOSTS_PATH",
                )
