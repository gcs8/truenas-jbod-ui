from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import paramiko

from app.config import SSHConfig
from app.services.ssh_probe import AutoPinHostKeyPolicy, SSHProbe


class SSHProbeTests(unittest.TestCase):
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
                    host="archive-core.gcs8.io",
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
                host="archive-core.gcs8.io",
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
                host="archive-core.gcs8.io",
                user="jbodmap",
                key_path="/run/ssh/id_truenas",
                password="",
                strict_host_key_checking=False,
            )
        )

        probe._client()

        ssh_client.connect.assert_called_once_with(
            hostname="archive-core.gcs8.io",
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
