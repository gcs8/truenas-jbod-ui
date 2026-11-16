from __future__ import annotations

import errno
import json
import shlex
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException,
    BadHostKeyException,
    NoValidConnectionsError,
    SSHException,
)

from tests.test_ssh_probe import MemorySSHWire

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.log_limiter import RepeatedLogLimiter
from app.models.domain import SasFabricSnapshot
from app.services import ssh_probe as ssh_probe_module
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.ssh_probe import SSHCommandResult, SSHProbe, redact_ssh_command
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import TrueNASRawData


class SSHDiagnosticSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_failed_command_projection_uses_only_fixed_context(self) -> None:
        private_marker = "SYNTHETIC-PRIVATE-DIAGNOSTIC"
        results = [
            SSHCommandResult(command="printf inventory", ok=True, stdout="inventory"),
            SSHCommandResult(
                command=f"tool -H 'Authorization: Bearer {private_marker}'",
                ok=False,
                stdout=f"stdout-{private_marker}",
                stderr=f"stderr-{private_marker}",
                exit_code=23,
            ),
        ]

        details = InventoryService._ssh_failure_details(results)

        self.assertEqual(
            details,
            [
                {
                    "reason": "ssh_command_failed",
                    "exit_code": 23,
                    "context": "ssh_command_2",
                    "context_label": "SSH command 2",
                    "criticality": "inventory",
                }
            ],
        )
        public_payload = SasFabricSnapshot(raw={"command_failures": details}).model_dump(mode="json")
        encoded = json.dumps(public_payload, sort_keys=True)
        self.assertNotIn(private_marker, encoded)
        for removed_field in ("command", "canonical_command", "stdout", "stderr"):
            self.assertNotIn(removed_field, details[0])

    def test_public_failure_summaries_do_not_copy_command_or_remote_text(self) -> None:
        private_marker = "SYNTHETIC-REMOTE-DIAGNOSTIC"
        cases = {
            "transport": (
                _refused_probe_results(["inventory first", "inventory second"], private_marker),
                {},
                [
                    "Could not log in over SSH (connection_refused). "
                    "Bay details from SSH are missing for this refresh."
                ],
                "SSH login failed (connection_refused).",
            ),
            "remote_exit_255": (
                [
                    SSHCommandResult(
                        command="inventory first",
                        ok=False,
                        stderr=private_marker,
                        exit_code=255,
                    ),
                    SSHCommandResult(
                        command="inventory second",
                        ok=False,
                        stderr=private_marker,
                        exit_code=255,
                    ),
                ],
                {},
                [
                    "SSH command failed (SSH command 1; ssh_command_failed; exit 255).",
                    "SSH command failed (SSH command 2; ssh_command_failed; exit 255).",
                ],
                "Some SSH commands failed.",
            ),
            "missing_storcli": (
                [
                    SSHCommandResult(
                        command="/opt/lsi/storcli64/storcli64 /c0 show all J",
                        ok=False,
                        stderr=private_marker,
                        exit_code=127,
                    )
                ],
                {"esxcli storage core adapter list": "synthetic adapter"},
                [
                    "StorCLI is not available on this ESXi host, so drive details behind the RAID "
                    "controller are missing for this refresh."
                ],
                "StorCLI commands unavailable.",
            ),
            "generic": (
                [
                    SSHCommandResult(
                        command=f"tool Bearer {private_marker}",
                        ok=False,
                        stderr=private_marker,
                        exit_code=23,
                    )
                ],
                {"inventory control": "available"},
                ["SSH command failed (SSH command 1; ssh_command_failed; exit 23)."],
                "Some SSH commands failed.",
            ),
        }

        for name, (results, outputs, expected_warnings, expected_status) in cases.items():
            with self.subTest(name=name):
                warnings, status = InventoryService._summarize_ssh_failures(results, outputs)
                self.assertEqual(warnings, expected_warnings)
                self.assertEqual(status, expected_status)
                self.assertNotIn(private_marker, json.dumps([warnings, status]))

    def test_log_redaction_masks_credential_values(self) -> None:
        private_marker = "SYNTHETIC-AUTH-VALUE"
        cases = {
            "authorization_header": (
                f"curl -H 'Authorization: Bearer {private_marker}' https://service.example.test",
                "Authorization: Bearer ***",
            ),
            "multiline_header": (
                f"curl -H 'Authorization: Bearer {private_marker}\ncontinued' https://service.example.test",
                "Authorization: Bearer ***",
            ),
            "cookie_header": (
                f"curl --header='Cookie: session={private_marker}; theme=light' https://service.example.test",
                "Cookie: ***",
            ),
            "cookie_option": (
                f"curl --cookie {private_marker} https://service.example.test",
                "--cookie '***'",
            ),
            "url_userinfo": (
                f"curl https://operator:{private_marker}@service.example.test/path",
                "https://operator:***@service.example.test/path",
            ),
            "unparsed_header": (
                f"curl -H 'Authorization: Bearer {private_marker}",
                "Authorization: Bearer ***",
            ),
        }

        for name, (command, safe_fragment) in cases.items():
            with self.subTest(name=name):
                redacted = redact_ssh_command(command)
                self.assertNotIn(private_marker, redacted)
                self.assertIn(safe_fragment, redacted)
                self.assertEqual(redact_ssh_command(redacted), redacted)

    def _make_service(
        self,
        platform: Literal["core", "scale", "linux", "quantastor", "esxi", "ipmi"],
    ) -> InventoryService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        raw = TrueNASRawData(
            enclosures=[],
            disks=[{"name": "sda", "serial": "SANITIZED-DISK"}],
            pools=[],
            disk_temperatures={},
            smart_test_results=[],
        )
        system = SystemConfig(
            id=f"synthetic-{platform}",
            truenas=TrueNASConfig(platform=platform),
            ssh=SSHConfig(enabled=True, host="node.example.test", commands=[]),
        )
        api = AsyncMock()
        api.fetch_all.return_value = raw
        ssh = AsyncMock()
        ssh.run_planned_commands.return_value = [
            SSHCommandResult(command="synthetic-base", ok=True, stdout="base-output", exit_code=0)
        ]
        settings = Settings()
        return InventoryService(
            settings,
            system,
            api,
            ssh,
            None,
            MappingStore(str(Path(directory.name) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(directory.name) / "slot_detail_cache.json")),
        )

    async def _assert_fabric_warning(
        self,
        service: InventoryService,
        *,
        marker: str,
        expected: str,
    ) -> None:
        fabric = await service.get_sas_fabric_snapshot(force_refresh=True)
        encoded = json.dumps(fabric.model_dump(mode="json"), sort_keys=True)
        self.assertNotIn(marker, encoded)
        self.assertIn(expected, encoded)

    async def test_overlay_failures_are_value_safe_through_the_fabric_snapshot(self) -> None:
        marker = "SYNTHETIC-REMOTE-DIAGNOSTIC"

        with self.subTest(path="scale_discovery"):
            service = self._make_service("scale")
            discovery = service._build_sg_ses_discovery_command()
            service._run_ssh_planned_commands = AsyncMock(
                return_value=_refused_probe_results([discovery], marker)
            )
            await self._assert_fabric_warning(
                service,
                marker=marker,
                expected=(
                    "TrueNAS SCALE SSH SES discovery failed on node.example.test "
                    "(connection_refused; exit 255)."
                ),
            )

        with self.subTest(path="scale_aes_page"):
            service = self._make_service("scale")
            discovery = service._build_sg_ses_discovery_command()
            device = "/dev/sg0"
            aes_command = shlex.join(["sudo", "-n", "/usr/bin/sg_ses", "-p", "aes", device])
            service._run_ssh_planned_commands = AsyncMock(
                return_value=[
                    SSHCommandResult(command=discovery, ok=True, stdout=f"{device}\n", exit_code=0),
                    SSHCommandResult(
                        command=aes_command,
                        ok=False,
                        stderr=f"permission denied: {marker}",
                        exit_code=1,
                    ),
                ]
            )
            await self._assert_fabric_warning(
                service,
                marker=marker,
                expected=(
                    "TrueNAS SCALE SSH SES AES page probe failed on node.example.test /dev/sg0 "
                    "(permission_denied; exit 1)."
                ),
            )

        with self.subTest(path="quantastor_cli_command"):
            service = self._make_service("quantastor")

            async def cli_results(commands, _host, *, stdin_data=None):
                del stdin_data
                return [
                    SSHCommandResult(
                        command=command,
                        ok=index != 0,
                        stdout="[]" if index else "",
                        stderr=f"command not found: {marker}" if index == 0 else "",
                        exit_code=127 if index == 0 else 0,
                    )
                    for index, command in enumerate(commands)
                ]

            service._run_ssh_commands = AsyncMock(side_effect=cli_results)
            service._fetch_quantastor_ses_overlay = AsyncMock(return_value=(ParsedSSHData(), []))
            await self._assert_fabric_warning(
                service,
                marker=marker,
                expected=(
                    "Quantastor SSH CLI disk inventory failed on node.example.test "
                    "(command_not_found; exit 127)."
                ),
            )

        with self.subTest(path="quantastor_cli_transport"):
            service = self._make_service("quantastor")

            async def transport_results(commands, _host, *, stdin_data=None):
                del stdin_data
                return _refused_probe_results(commands, marker)

            service._run_ssh_commands = AsyncMock(side_effect=transport_results)
            service._fetch_quantastor_ses_overlay = AsyncMock(return_value=(ParsedSSHData(), []))
            await self._assert_fabric_warning(
                service,
                marker=marker,
                expected=(
                    "Quantastor SSH CLI enrichment failed on node.example.test "
                    "(connection_refused; exit 255). REST data is still being used."
                ),
            )

        with self.subTest(path="quantastor_ses_transport"):
            service = self._make_service("quantastor")
            discovery = service._build_sg_ses_discovery_command()
            service._fetch_quantastor_cli_overlay = AsyncMock(
                return_value=(
                    {
                        "cli_disks": [],
                        "cli_hw_disks": [],
                        "cli_hw_enclosures": [],
                        "cli_network_ports": [],
                    },
                    [],
                )
            )
            service._run_ssh_planned_commands = AsyncMock(
                return_value=_refused_probe_results([discovery], marker)
            )
            await self._assert_fabric_warning(
                service,
                marker=marker,
                expected=(
                    "Quantastor SSH SES discovery failed on node.example.test "
                    "(connection_refused; exit 255)."
                ),
            )

    def test_redaction_preserves_benign_unit_device_and_prose_values(self) -> None:
        commands = [
            "sudo -n /usr/sbin/mprutil -u 10 show iocfacts",
            "sudo -n /usr/sbin/sesutil map -u /dev/ses0",
            "grep -b token inventory.txt",
            "echo basic value done",
            "grep -i token inventory.txt",
        ]

        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(redact_ssh_command(command), command)

    def test_ambiguous_options_are_redacted_only_when_credential_bearing(self) -> None:
        private_marker = "SYNTHETIC-AUTH-VALUE"
        cases = [
            f"curl -u operator:{private_marker} https://service.example.test",
            f"curl -b session={private_marker} https://service.example.test",
            f"client -u operator:{private_marker}",
        ]

        for command in cases:
            with self.subTest(command=command):
                self.assertNotIn(private_marker, redact_ssh_command(command))

    def test_login_failure_summaries_keep_only_fixed_reason_categories(self) -> None:
        # Transport categories come from the exception type and errno; see
        # SSHFailureOriginTests. Executed commands keep their list position.
        command_results = [
            SSHCommandResult(command="synthetic control", ok=True, stdout="ok", exit_code=0),
            SSHCommandResult(command="synthetic first", ok=False, stderr="failure one", exit_code=1),
            SSHCommandResult(command="synthetic second", ok=False, stderr="failure two", exit_code=2),
        ]
        warnings, _status = InventoryService._summarize_ssh_failures(
            command_results,
            {"synthetic control": "ok"},
        )
        projected = InventoryService._ssh_failure_details(command_results)
        self.assertEqual(
            warnings,
            [
                "SSH command failed (SSH command 2; ssh_command_failed; exit 1).",
                "SSH command failed (SSH command 3; ssh_command_failed; exit 2).",
            ],
        )
        self.assertEqual(
            [item["context"] for item in projected],
            ["ssh_command_2", "ssh_command_3"],
        )

    def test_distinct_controller_failures_log_value_safe_reasons_separately(self) -> None:
        marker = "SYNTHETIC-COMMAND-DETAIL"
        probe = SSHProbe(SSHConfig(enabled=True, host="node.example.test"))
        commands = [
            "sudo -n /usr/sbin/mprutil -u 10 show iocfacts",
            "sudo -n /usr/sbin/mprutil -u 11 show iocfacts",
        ]
        limiter = RepeatedLogLimiter(repeat_interval_seconds=None)

        with (
            patch.object(ssh_probe_module, "SSH_COMMAND_FAILURE_LOG", limiter),
            self.assertLogs("app.services.ssh_probe", level="WARNING") as logs,
        ):
            for command in commands:
                wire = MemorySSHWire(
                    lambda channel, _command: MemorySSHWire.feed(
                        channel,
                        error=f"permission denied: {marker}".encode(),
                        eof=True,
                        status=1,
                    )
                )
                result = probe._run_single_command(wire.client, command)
                wire.close()
                self.assertFalse(result.ok)

        self.assertEqual(len(logs.output), 2)
        self.assertIn("-u 10", logs.output[0])
        self.assertIn("-u 11", logs.output[1])
        for line in logs.output:
            self.assertIn("reason=permission_denied", line)
            self.assertNotIn(marker, line)


ORIGIN_MARKER = "SYNTHETIC-ORIGIN-DETAIL"


def _refused_probe_results(commands, marker: str = ORIGIN_MARKER) -> list[SSHCommandResult]:
    """Results the real probe returns when the connection is refused."""

    probe = SSHProbe(
        SSHConfig(
            enabled=True,
            host="node.example.test",
            user="synthetic-operator",
            key_path="",
            known_hosts_path="",
        )
    )
    refused = NoValidConnectionsError({("192.0.2.10", 22): ConnectionRefusedError(errno.ECONNREFUSED, marker)})
    with (
        patch.object(paramiko.SSHClient, "connect", _raise(refused)),
        patch.object(ssh_probe_module.logger, "warning"),
    ):
        return probe._run_commands_sync(list(commands))


def _raise(exc: BaseException):
    def connect(self, **_kwargs):
        raise exc

    return connect


def _auth_with_key_file(self, **kwargs):
    # The installed client's own key-file authentication, as connect() runs it
    # after the handshake; no transport is touched before a key loads.
    self._auth(kwargs["username"], None, None, [kwargs["key_filename"]], False, False, None)


def _reject_unknown_host_key(self, **kwargs):
    # The installed reject policy, as the handshake calls it for an unknown key.
    self._log = lambda *_args, **_kwargs: None
    self._policy.missing_host_key(self, kwargs["hostname"], paramiko.RSAKey.generate(1024))


def _encrypted_key_file(directory: str) -> str:
    path = str(Path(directory) / "encrypted_key")
    paramiko.RSAKey.generate(1024).write_private_key_file(path, password=ORIGIN_MARKER)
    return path


class SSHFailureOriginTests(unittest.IsolatedAsyncioTestCase):
    """Failure categories come from structure: exception type, errno, local flags."""

    def setUp(self) -> None:
        for limiter in ssh_probe_module.SSH_LOG_LIMITERS:
            limiter.reset()
        self.addCleanup(lambda: [limiter.reset() for limiter in ssh_probe_module.SSH_LOG_LIMITERS])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def _probe(self, key_path: str = "") -> SSHProbe:
        return SSHProbe(
            SSHConfig(
                enabled=True,
                host="node.example.test",
                user="synthetic-operator",
                key_path=key_path,
                known_hosts_path="",
            )
        )

    def _connect_failure(self, connect, *, key_path: str = "", level: str = "WARNING"):
        probe = self._probe(key_path)
        with (
            patch.object(paramiko.SSHClient, "connect", connect),
            self.assertLogs("app.services.ssh_probe", level=level) as logs,
        ):
            results = probe._run_commands_sync(["synthetic-first", "synthetic-second"])
        return results, logs

    def _transport_cases(self) -> list[tuple[str, object, str, str, int | None, str]]:
        missing_key = str(Path(self.directory) / f"missing-{ORIGIN_MARKER}")
        invalid_key = Path(self.directory) / "invalid_key"
        invalid_key.write_text(f"not a key {ORIGIN_MARKER}\n", encoding="utf-8")
        address = ("192.0.2.10", 22)
        return [
            (
                "connection_refused",
                _raise(NoValidConnectionsError({address: ConnectionRefusedError(errno.ECONNREFUSED, ORIGIN_MARKER)})),
                "",
                "NoValidConnectionsError",
                errno.ECONNREFUSED,
                "connection_refused",
            ),
            (
                "host_unreachable",
                _raise(NoValidConnectionsError({address: OSError(errno.EHOSTUNREACH, ORIGIN_MARKER)})),
                "",
                "NoValidConnectionsError",
                errno.EHOSTUNREACH,
                "host_unreachable",
            ),
            (
                "network_unreachable",
                _raise(OSError(errno.ENETUNREACH, ORIGIN_MARKER)),
                "",
                "OSError",
                errno.ENETUNREACH,
                "host_unreachable",
            ),
            (
                "dns_failed",
                _raise(socket.gaierror(socket.EAI_NONAME, ORIGIN_MARKER)),
                "",
                "gaierror",
                socket.EAI_NONAME,
                "dns_failed",
            ),
            ("timeout", _raise(socket.timeout(ORIGIN_MARKER)), "", "TimeoutError", None, "timeout"),
            (
                "key_file_missing",
                _auth_with_key_file,
                missing_key,
                "FileNotFoundError",
                errno.ENOENT,
                "key_file_missing",
            ),
            (
                "key_file_invalid",
                _auth_with_key_file,
                str(invalid_key),
                "SSHException",
                None,
                "key_file_invalid",
            ),
            (
                "key_file_passphrase",
                _auth_with_key_file,
                _encrypted_key_file(self.directory),
                # The client tries each key class and re-raises the last error.
                "SSHException",
                None,
                "key_file_invalid",
            ),
            (
                "key_file_unreadable",
                _auth_with_key_file,
                self.directory,
                "IsADirectoryError",
                errno.EISDIR,
                "key_file_unreadable",
            ),
            ("host_key_unknown", _reject_unknown_host_key, "", "SSHException", None, "host_key_rejected"),
            (
                "host_key_changed",
                _raise(
                    BadHostKeyException(
                        "node.example.test", paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)
                    )
                ),
                "",
                "BadHostKeyException",
                None,
                "host_key_rejected",
            ),
            ("auth_failed", _raise(AuthenticationException(ORIGIN_MARKER)), "", "AuthenticationException", None, "auth_failed"),
            (
                "auth_type",
                _raise(paramiko.BadAuthenticationType(ORIGIN_MARKER, ["publickey"])),
                "",
                "BadAuthenticationType",
                None,
                "auth_failed",
            ),
            ("protocol_banner", _raise(SSHException(ORIGIN_MARKER)), "", "SSHException", None, "protocol_error"),
            ("protocol_eof", _raise(EOFError(ORIGIN_MARKER)), "", "EOFError", None, "protocol_error"),
            (
                "unknown_socket_error",
                _raise(OSError(errno.ECONNRESET, ORIGIN_MARKER)),
                "",
                "ConnectionResetError",
                errno.ECONNRESET,
                "ssh_transport_failed",
            ),
        ]

    def test_transport_failures_are_classified_by_exception_type_and_errno(self) -> None:
        for name, connect, key_path, type_name, errno_value, reason in self._transport_cases():
            with self.subTest(case=name):
                results, logs = self._connect_failure(connect, key_path=key_path)

                self.assertEqual(len(results), 2)
                self.assertEqual(
                    [getattr(getattr(result, "local_failure", None), "reason", None) for result in results],
                    [reason, reason],
                )
                self.assertEqual(len(logs.output), 1, logs.output)
                line = logs.output[0]
                self.assertTrue(line.startswith("WARNING:"), line)
                self.assertIn("SSH command batch failed for synthetic-operator@node.example.test", line)
                self.assertIn(
                    f"reason={reason} type={type_name} errno={'-' if errno_value is None else errno_value}",
                    line,
                )
                self.assertNotIn(ORIGIN_MARKER, line)

                warnings, status = InventoryService._summarize_ssh_failures(results, {})
                self.assertEqual(
                    warnings,
                    [f"Could not log in over SSH ({reason}). Bay details from SSH are missing for this refresh."],
                )
                self.assertEqual(status, f"SSH login failed ({reason}).")
                details = InventoryService._ssh_failure_details(results)
                self.assertEqual({detail["reason"] for detail in details}, {reason})
                self.assertNotIn(ORIGIN_MARKER, json.dumps([warnings, status, details]))

    def test_every_transport_log_site_carries_type_and_errno(self) -> None:
        refused = NoValidConnectionsError(
            {("192.0.2.10", 22): ConnectionRefusedError(errno.ECONNREFUSED, ORIGIN_MARKER)}
        )
        expected = f"reason=connection_refused type=NoValidConnectionsError errno={errno.ECONNREFUSED}"
        sites = {
            "batch": lambda probe: probe._run_commands_sync(["synthetic"]),
            "planned_session": lambda probe: probe._run_planned_command_groups_sync(
                [((lambda _results: []), ["synthetic"])]
            ),
            "single_command": lambda probe: probe._run_command_sync("synthetic"),
            "session_start": lambda probe: probe.open_session().run_command("synthetic"),
        }
        for name, run in sites.items():
            with self.subTest(site=name):
                probe = self._probe()
                with (
                    patch.object(paramiko.SSHClient, "connect", _raise(refused)),
                    self.assertLogs("app.services.ssh_probe", level="WARNING") as logs,
                ):
                    run(probe)
                self.assertEqual(len(logs.output), 1, logs.output)
                self.assertIn(expected, logs.output[0])
                self.assertNotIn(ORIGIN_MARKER, logs.output[0])

        with self.subTest(site="execution"):
            client = MagicMock()
            client.exec_command.side_effect = SSHException(ORIGIN_MARKER)
            with self.assertLogs("app.services.ssh_probe", level="WARNING") as logs:
                result = self._probe()._run_single_command(client, "synthetic")
            self.assertEqual(getattr(getattr(result, "local_failure", None), "reason", None), "protocol_error")
            self.assertIn("SSH command execution failed", logs.output[0])
            self.assertIn("reason=protocol_error type=SSHException errno=-", logs.output[0])
            self.assertNotIn(ORIGIN_MARKER, logs.output[0])

        with self.subTest(site="channel"):
            client = MagicMock()
            client.exec_command.side_effect = paramiko.ChannelException(2, ORIGIN_MARKER)
            with self.assertLogs("app.services.ssh_probe", level="WARNING") as logs:
                result = self._probe()._run_single_command(client, "synthetic")
            self.assertEqual(getattr(getattr(result, "local_failure", None), "reason", None), "channel_refused")
            self.assertIn("reason=channel_refused type=ChannelException errno=-", logs.output[0])
            self.assertNotIn(ORIGIN_MARKER, logs.output[0])

        with self.subTest(site="deadline"):
            client = MagicMock()
            client.exec_command.side_effect = socket.timeout(ORIGIN_MARKER)
            with self.assertLogs("app.services.ssh_probe", level="WARNING") as logs:
                result = self._probe()._run_single_command(client, "synthetic")
            self.assertEqual(getattr(getattr(result, "local_failure", None), "reason", None), "timeout")
            self.assertIn("reason=timeout type=TimeoutError errno=-", logs.output[0])

    def test_unexpected_local_exception_is_internal_error_with_frames_only(self) -> None:
        results, logs = self._connect_failure(_raise(RuntimeError(ORIGIN_MARKER)))

        self.assertEqual(
            [getattr(getattr(result, "local_failure", None), "reason", None) for result in results],
            ["internal_error", "internal_error"],
        )
        self.assertEqual(len(logs.records), 1, logs.output)
        record = logs.records[0]
        self.assertEqual(record.levelname, "ERROR")
        line = record.getMessage()
        self.assertIn("reason=internal_error type=RuntimeError", line)
        self.assertIn("in connect", line)
        self.assertIn("raise exc", line)
        self.assertNotIn(ORIGIN_MARKER, line)
        self.assertIsNone(record.exc_info)

        warnings, status = InventoryService._summarize_ssh_failures(results, {})
        self.assertEqual(
            warnings,
            [
                "SSH collection stopped on an internal error (internal_error). "
                "Bay details from SSH are missing for this refresh."
            ],
        )
        self.assertEqual(status, "SSH collection error (internal_error).")
        self.assertNotIn("appliance", " ".join(warnings + [status]).lower())
        self.assertNotIn(ORIGIN_MARKER, json.dumps([warnings, status]))

        # A programming error inside the host-key check is still a local bug,
        # not a rejected host key.
        def broken_host_key_check(client, **kwargs):
            client._transport = None
            client._policy.missing_host_key(client, kwargs["hostname"], paramiko.RSAKey.generate(1024))

        results, logs = self._connect_failure(broken_host_key_check)
        self.assertEqual(results[0].local_failure.reason, "internal_error")
        self.assertEqual(logs.records[0].levelname, "ERROR")
        self.assertIn("type=AttributeError", logs.records[0].getMessage())

    def test_executed_command_reason_uses_stderr_and_exit_status_only(self) -> None:
        cases = [
            ({"exit_code": 1, "stdout": '{"Controllers":[{"Timeout":30}]}'}, "ssh_command_failed"),
            ({"exit_code": 2, "stdout": "Host key fingerprint listing"}, "ssh_command_failed"),
            ({"exit_code": 1, "stdout": "permission denied entries: 0"}, "ssh_command_failed"),
            ({"exit_code": 1, "stderr": "Connection timed out while reading"}, "ssh_command_failed"),
            ({"exit_code": 255, "stderr": "Connection refused"}, "ssh_command_failed"),
            ({"exit_code": 255, "stderr": "Authentication failed."}, "ssh_command_failed"),
            ({"exit_code": 127, "stderr": ""}, "command_not_found"),
            ({"exit_code": 1, "stderr": "sudo: a password is required"}, "sudo_password_required"),
            ({"exit_code": 1, "stderr": "open failed: Permission denied"}, "permission_denied"),
        ]
        for fields, reason in cases:
            with self.subTest(fields=fields):
                result = SSHCommandResult(command="synthetic", ok=False, **fields)
                self.assertEqual(InventoryService._ssh_failure_details([result])[0]["reason"], reason)

        remote_exits = [
            SSHCommandResult(command=f"synthetic {index}", ok=False, stderr=f"Connection refused {index}", exit_code=255)
            for index in range(2)
        ]
        warnings, status = InventoryService._summarize_ssh_failures(remote_exits, {})
        self.assertEqual(
            warnings,
            [
                "SSH command failed (SSH command 1; ssh_command_failed; exit 255).",
                "SSH command failed (SSH command 2; ssh_command_failed; exit 255).",
            ],
        )
        self.assertEqual(status, "Some SSH commands failed.")

        limiter = RepeatedLogLimiter(repeat_interval_seconds=None)
        with (
            patch.object(ssh_probe_module, "SSH_COMMAND_FAILURE_LOG", limiter),
            self.assertLogs("app.services.ssh_probe", level="WARNING") as logs,
        ):
            wire = MemorySSHWire(
                lambda channel, _command: MemorySSHWire.feed(
                    channel, output=b'{"Timeout": 30}', error=b"", eof=True, status=1
                )
            )
            self._probe()._run_single_command(wire.client, "synthetic-read")
            wire.close()
        self.assertIn("reason=ssh_command_failed", logs.output[0])

    def _service(self, platform: Literal["scale", "quantastor"] = "quantastor") -> InventoryService:
        settings = Settings()
        system = SystemConfig(
            id=f"synthetic-{platform}",
            truenas=TrueNASConfig(platform=platform),
            ssh=SSHConfig(enabled=True, host="node.example.test", user="synthetic-operator", commands=[]),
        )
        return InventoryService(
            settings,
            system,
            AsyncMock(),
            SSHProbe(system.ssh),
            None,
            MappingStore(str(Path(self.directory) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(self.directory) / "slot_detail_cache.json")),
        )

    async def test_unapproved_destination_has_its_own_reason_and_log_line(self) -> None:
        service = self._service()
        limiter = RepeatedLogLimiter(repeat_interval_seconds=900.0)
        hosts = {
            "unapproved.example.test": "unapproved.example.test",
            "198.51.100.7": "198.51.100.7",
            f"bad host {ORIGIN_MARKER}\nsecond line": "<unparsed-host>",
        }
        for host, logged_host in hosts.items():
            with self.subTest(host=logged_host):
                with (
                    patch.object(ssh_probe_module, "SSH_COMMAND_FAILURE_LOG", limiter),
                    patch.object(SSHProbe, "run_commands", new=AsyncMock(side_effect=AssertionError("no transport"))),
                    self.assertLogs("app.services.inventory", level="WARNING") as logs,
                ):
                    results = await service._run_ssh_planned_commands(
                        lambda _results: [], initial_commands=["synthetic-cmd"], host=host
                    )
                self.assertEqual(
                    getattr(getattr(results[0], "local_failure", None), "reason", None),
                    "destination_not_approved",
                )
                failure = service._optional_ssh_transport_failure(results)
                self.assertIsNotNone(failure)
                self.assertEqual(
                    service._format_optional_ssh_transport_failure(failure),
                    "destination_not_approved; exit 255",
                )
                refusal_lines = [line for line in logs.output if "destination_not_approved" in line]
                self.assertEqual(len(refusal_lines), 1, logs.output)
                self.assertTrue(refusal_lines[0].startswith("WARNING:"), refusal_lines[0])
                self.assertIn("system=synthetic-quantastor", refusal_lines[0])
                self.assertIn(f"host={logged_host}", refusal_lines[0])
                self.assertNotIn(ORIGIN_MARKER, refusal_lines[0])
                self.assertNotIn("\n", refusal_lines[0])

        with patch.object(ssh_probe_module, "SSH_COMMAND_FAILURE_LOG", limiter):
            with self.assertNoLogs("app.services.inventory", level="WARNING"):
                await service._run_ssh_commands(["synthetic-cmd"], "unapproved.example.test")

    async def test_backoff_retry_time_comes_from_local_state(self) -> None:
        service = self._service()
        retry_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        service._optional_ssh_backoff_until["node.example.test"] = retry_at

        with patch.object(SSHProbe, "run_commands", new=AsyncMock(side_effect=AssertionError("no transport"))):
            results = await service._run_ssh_commands(["synthetic-a", "synthetic-b"], "node.example.test")
        local = getattr(results[0], "local_failure", None)
        self.assertEqual(getattr(local, "reason", None), "retry_backoff")
        self.assertEqual(getattr(local, "retry_after", None), retry_at)
        for result in results:
            result.stderr = f"retry after {ORIGIN_MARKER}"
        failure = service._optional_ssh_transport_failure(results)
        self.assertEqual(
            service._format_optional_ssh_transport_failure(failure),
            f"retry_backoff; exit 255; retry after {retry_at.isoformat()}",
        )

        lookalike = (
            "Skipping optional SSH command batch after a recent connection startup failure; "
            f"retry after {ORIGIN_MARKER}-{'x' * 300}."
        )
        remote = [SSHCommandResult(command="synthetic", ok=False, stderr=lookalike, exit_code=255)]
        self.assertIsNone(service._optional_ssh_transport_failure(remote))
        self.assertIsNone(service._optional_ssh_transport_failure([results[0], remote[0]]))
        self.assertEqual(service._ssh_failure_public_summary(remote[0]), "ssh_command_failed; exit 255")

    async def test_remote_exit_text_does_not_start_a_local_backoff(self) -> None:
        service = self._service()
        remote = [
            SSHCommandResult(command="synthetic", ok=False, stderr="Connection refused", exit_code=255),
        ]
        with patch.object(SSHProbe, "run_commands", new=AsyncMock(return_value=remote)):
            await service._run_ssh_commands(["synthetic"], "node.example.test")
        self.assertEqual(service._optional_ssh_backoff_until, {})

        refused = NoValidConnectionsError(
            {("192.0.2.10", 22): ConnectionRefusedError(errno.ECONNREFUSED, ORIGIN_MARKER)}
        )
        with patch.object(paramiko.SSHClient, "connect", _raise(refused)), self.assertLogs(level="WARNING"):
            await service._run_ssh_commands(["synthetic"], "node.example.test")
        self.assertIn("node.example.test", service._optional_ssh_backoff_until)

    def test_expander_chunk_no_response_comes_from_the_local_deadline(self) -> None:
        sas_address = "0" * 15 + "1"  # synthetic expander address
        identity = f"1:{sas_address}:pass0:00-07"
        command = f"echo tnj-smp-phy-error-log:{identity} synthetic-read"
        client = MagicMock()
        client.exec_command.side_effect = socket.timeout(ORIGIN_MARKER)
        with self.assertLogs("app.services.ssh_probe", level="WARNING"):
            local_timeout = self._probe()._run_single_command(client, command)
        remote_text = SSHCommandResult(command=command, ok=False, stderr="operation timed out", exit_code=1)

        for result, expected in ((local_timeout, "no_response"), (remote_text, "command_failed")):
            with self.subTest(expected=expected):
                service = self._service()
                expander = (1, sas_address)
                service._smp_counter_planned = {f"smp phy error log {identity}": expander}
                service._smp_counter_queue = {expander: ["synthetic-next-chunk"]}
                service._smp_counter_stopped = {}
                self.assertEqual(service._smp_counter_next_chunks([result]), [])
                self.assertEqual(service._smp_counter_stopped[expander], (expected, 0))

    async def test_backoff_collapse_uses_current_producer_strings(self) -> None:
        settings = Settings()
        system = SystemConfig(
            id="synthetic-quantastor",
            truenas=TrueNASConfig(platform="quantastor", api_user="synthetic", api_password="synthetic"),
            ssh=SSHConfig(enabled=True, host="192.0.2.30", user="synthetic-operator", commands=[]),
        )
        service = InventoryService(
            settings,
            system,
            AsyncMock(),
            SSHProbe(system.ssh),
            None,
            MappingStore(str(Path(self.directory) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(self.directory) / "slot_detail_cache.json")),
        )
        retry_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        service._optional_ssh_backoff_until["192.0.2.30"] = retry_at

        with patch.object(SSHProbe, "run_commands", new=AsyncMock(side_effect=AssertionError("no transport"))):
            _overlay, cli_failures = await service._fetch_quantastor_cli_overlay()
            _devices, _ses, ses_failures = await service._discover_and_fetch_sg_ses_host_overlay(
                "192.0.2.30", failure_prefix="Quantastor SSH SES"
            )
        self.assertEqual(
            cli_failures,
            [
                f"Quantastor SSH CLI enrichment failed on 192.0.2.30 (retry_backoff; exit 255; "
                f"retry after {retry_at.isoformat()}). REST data is still being used."
            ],
        )
        self.assertEqual(
            ses_failures,
            [
                f"Quantastor SSH SES discovery failed on 192.0.2.30 (retry_backoff; exit 255; "
                f"retry after {retry_at.isoformat()})."
            ],
        )
        collapsed = InventoryService._collapse_quantastor_optional_ssh_backoff_warnings(
            [*cli_failures, *ses_failures, "Unrelated warning."]
        )
        self.assertEqual(
            collapsed,
            [
                f"SSH to 192.0.2.30 (retry after {retry_at.isoformat()}) is paused after a connection "
                "failure. Data still comes from the QuantaStor API.",
                "Unrelated warning.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
