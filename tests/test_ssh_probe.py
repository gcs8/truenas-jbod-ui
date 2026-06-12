from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import paramiko

from app.config import SSHConfig
from app.services.ssh_probe import AutoPinHostKeyPolicy, SSHCommandResult, SSHProbe, redact_ssh_command


class SSHProbeTests(unittest.TestCase):
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
    def test_client_uses_tofu_host_key_pinning_by_default(
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
                    host="archive-core.example.test",
                    user="jbodmap",
                    key_path="/run/ssh/id_truenas",
                )
            )

            probe._client()

        prepare_known_hosts_path.assert_called_once_with(default_known_hosts_path)
        ssh_client.load_host_keys.assert_called_once_with(default_known_hosts_path)
        policy = ssh_client.set_missing_host_key_policy.call_args.args[0]
        self.assertIsInstance(policy, AutoPinHostKeyPolicy)
        self.assertEqual(policy.known_hosts_path, default_known_hosts_path)

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
            stdin = MagicMock()
            stdout = MagicMock()
            stderr = MagicMock()
            stdout.read.return_value = f"{command} output".encode()
            stderr.read.return_value = b""
            stdout.channel.recv_exit_status.return_value = 0
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
            stdin = MagicMock()
            stdout = MagicMock()
            stderr = MagicMock()
            stdout.read.return_value = b"ok"
            stderr.read.return_value = b""
            stdout.channel.recv_exit_status.return_value = 0
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
            stdin = MagicMock()
            stdout = MagicMock()
            stderr = MagicMock()
            stdout.read.return_value = f"{command} output".encode()
            stderr.read.return_value = b""
            stdout.channel.recv_exit_status.return_value = 0
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
