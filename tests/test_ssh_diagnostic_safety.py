from __future__ import annotations

import errno
import json
import logging
import re
import shlex
import socket
import sys
import tempfile
import unittest
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
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
from app.services import inventory as inventory_module
from app.services import ssh_probe as ssh_probe_module
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.ssh_probe import SSHCommandResult, SSHLocalFailure, SSHProbe, redact_ssh_command
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
        self.assertIn("test_ssh_diagnostic_safety.py:", line)
        self.assertIn(" in connect", line)
        self.assertNotIn("raise exc", line)
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

    async def test_optional_backoff_requires_local_failure_provenance(self) -> None:
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


VALUE_MARKER = "SYNTHETIC-REMOTE-VALUE"
REFUSAL_SENTENCE = "SSH target is not an operator-approved destination for this credential authority."
INTERNAL_SENTENCE = "The read stopped on an internal error in this service; the service log has the details."
FRAME_LINE_RE = re.compile(r"  [A-Za-z0-9_.<>-]{1,80}:[0-9]+ in [A-Za-z0-9_.<>]{1,80}")


def _sg_service(directory: str, platform: Literal["scale", "quantastor"] = "scale") -> InventoryService:
    settings = Settings()
    system = SystemConfig(
        id=f"synthetic-{platform}",
        truenas=TrueNASConfig(platform=platform),
        ssh=SSHConfig(enabled=True, host="node.example.test", user="synthetic-operator", commands=[]),
    )
    api = AsyncMock()
    api.fetch_all.return_value = TrueNASRawData(
        enclosures=[],
        disks=[{"name": "sda", "serial": "SANITIZED-DISK"}],
        pools=[],
        disk_temperatures={},
        smart_test_results=[],
    )
    ssh = AsyncMock()
    ssh.run_planned_commands.return_value = [
        SSHCommandResult(command="synthetic-base", ok=True, stdout="base-output", exit_code=0)
    ]
    return InventoryService(
        settings,
        system,
        api,
        ssh,
        None,
        MappingStore(str(Path(directory) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(directory) / "slot_detail_cache.json")),
    )


class RemoteValueAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """A value read from remote output is used or shown only after its grammar admits it."""

    def setUp(self) -> None:
        for limiter in ssh_probe_module.SSH_LOG_LIMITERS:
            limiter.reset()
        self.addCleanup(lambda: [limiter.reset() for limiter in ssh_probe_module.SSH_LOG_LIMITERS])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def _planned_through_real_probe(self, service: InventoryService, discovery_stdout: str, planned: list[str]):
        """Run the planner on a discovery answer; each planned page read fails on the real probe."""

        probe = SSHProbe(SSHConfig(enabled=True, host="node.example.test"))

        async def run_planned(planner, *, initial_commands, host):
            del host
            discovery = SSHCommandResult(
                command=initial_commands[0], ok=True, stdout=discovery_stdout, exit_code=0
            )
            commands = planner([discovery])
            planned.extend(commands)
            results = [discovery]
            for command in commands:
                wire = MemorySSHWire(
                    lambda channel, _command: MemorySSHWire.feed(
                        channel, error=b"permission denied", eof=True, status=1
                    )
                )
                try:
                    results.append(probe._run_single_command(wire.client, command))
                finally:
                    wire.close()
            return results

        service._run_ssh_planned_commands = run_planned

    async def test_discovered_sg_devices_are_admitted_only_in_the_device_form(self) -> None:
        service = _sg_service(self.directory)
        rejected_lines = [
            f"/dev/sg0-{VALUE_MARKER}",
            f"/dev/sg1 {VALUE_MARKER}",
            f"/dev/sg{VALUE_MARKER}",
            "/dev/sg01",
            "/dev/sg123456",
            f"{VALUE_MARKER} /dev/sg2",
            "/dev/sg\u0663",
        ]
        stdout = "\n".join([*rejected_lines, "/dev/sg3", "", "/dev/sg3"]) + "\n"
        planned: list[str] = []
        self._planned_through_real_probe(service, stdout, planned)
        limiter = RepeatedLogLimiter(repeat_interval_seconds=None)

        with (
            patch.object(ssh_probe_module, "SSH_COMMAND_FAILURE_LOG", limiter),
            self.assertLogs(level="INFO") as logs,
        ):
            snapshot = await service.get_sas_fabric_snapshot(force_refresh=True)
        encoded = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True)

        self.assertNotIn(VALUE_MARKER, encoded)
        self.assertIn(
            "TrueNAS SCALE SSH SES AES page probe failed on node.example.test /dev/sg3 "
            "(permission_denied; exit 1).",
            snapshot.warnings,
        )
        self.assertEqual(
            {shlex.split(command)[-1] for command in planned if "sg_ses" in command},
            {"/dev/sg3"},
        )
        rendered = [logging.Formatter("%(levelname)s:%(name)s:%(message)s").format(r) for r in logs.records]
        self.assertFalse([line for line in rendered if VALUE_MARKER in line], rendered)
        self.assertFalse([line for line in rendered if "/dev/sg0" in line or "/dev/sg01" in line], rendered)
        counted = [line for line in rendered if "operation=sg_ses_discovery" in line]
        self.assertEqual(len(counted), 1, rendered)
        self.assertTrue(counted[0].startswith("WARNING:app.services.inventory:"), counted[0])
        self.assertIn("ignored 7 lines", counted[0])
        self.assertIn("0 devices beyond the limit of 16384", counted[0])
        self.assertTrue(
            any(
                line.startswith("WARNING:app.services.ssh_probe:SSH command failed: ")
                and "/dev/sg3" in line
                for line in rendered
            ),
            rendered,
        )

    async def test_discovered_sg_device_count_is_bounded(self) -> None:
        service = _sg_service(self.directory)
        stdout = "".join(f"/dev/sg{index}\n" for index in range(70))
        planned: list[str] = []
        self._planned_through_real_probe(service, stdout, planned)
        with (
            patch.object(inventory_module, "SG_SES_MAX_DISCOVERED_DEVICES", 64),
            self.assertLogs("app.services", level="WARNING") as logs,
        ):
            devices, _overlay, failures = await service._discover_and_fetch_sg_ses_host_overlay(
                "node.example.test", failure_prefix="TrueNAS SCALE SSH SES"
            )
        self.assertEqual(devices, [f"/dev/sg{index}" for index in range(64)])
        self.assertIn(
            "TrueNAS SCALE SSH SES read is incomplete on node.example.test: 64 SG devices read, "
            "6 left out at the limit of 64 per host. Bays behind the devices left out are missing "
            "for this refresh.",
            failures,
        )
        planned_devices = {shlex.split(command)[-1] for command in planned if "sg_ses" in command}
        self.assertEqual(len(planned_devices), 64)
        counted = [line for line in logs.output if "operation=sg_ses_discovery" in line]
        self.assertEqual(len(counted), 1, logs.output)
        self.assertIn("ignored 0 lines", counted[0])
        self.assertIn("6 devices beyond the limit of 64", counted[0])

        with self.subTest(control="within_bound"):
            service = _sg_service(self.directory)
            self._planned_through_real_probe(service, "/dev/sg4\n/dev/sg5\n", [])
            with self.assertNoLogs("app.services.inventory", level="WARNING"):
                devices, _overlay, _failures = await service._discover_and_fetch_sg_ses_host_overlay(
                    "node.example.test", failure_prefix="TrueNAS SCALE SSH SES"
                )
            self.assertEqual(devices, ["/dev/sg4", "/dev/sg5"])

    def test_sg_device_cache_keeps_only_admitted_devices(self) -> None:
        service = _sg_service(self.directory)
        service._remember_sg_ses_devices(
            "node.example.test",
            [f"/dev/sg0-{VALUE_MARKER}", "/dev/sg4", *(f"/dev/sg{index}" for index in range(10, 80))],
        )
        cached = service._get_cached_sg_ses_devices("node.example.test")
        self.assertEqual(cached[0], "/dev/sg4")
        self.assertEqual(len(cached), 71)
        self.assertNotIn(VALUE_MARKER, json.dumps(cached))

    def test_appliance_reported_ssh_hosts_pass_the_host_grammar(self) -> None:
        row = {
            "sshHost": f"{VALUE_MARKER}.example.test;exit",
            "managementIpAddress": "node-b.example.test",
            "ipAddress": "192.0.2.41",
            "hostname": f"bad host {VALUE_MARKER}",
            "ipAddresses": [f"198.51.100.{index}" for index in range(1, 40)],
        }
        with self.assertLogs("app.services.inventory", level="WARNING") as logs:
            hosts = InventoryService._extract_quantastor_system_hosts(row)
        self.assertEqual(hosts[:2], ["node-b.example.test", "192.0.2.41"])
        self.assertEqual(len(hosts), 41)
        self.assertNotIn(VALUE_MARKER.lower(), json.dumps(hosts).lower())
        counted = [line for line in logs.output if "operation=quantastor_node_hosts" in line]
        self.assertEqual(len(counted), 1, logs.output)
        self.assertIn("ignored 2 values", counted[0])
        self.assertIn("0 beyond the limit of 256", counted[0])
        self.assertNotIn(VALUE_MARKER.lower(), counted[0].lower())

        control = {"ipAddress": "192.0.2.42", "hostname": "node-c.example.test"}
        with self.assertNoLogs("app.services.inventory", level="WARNING"):
            self.assertEqual(
                InventoryService._extract_quantastor_system_hosts(control),
                ["192.0.2.42", "node-c.example.test"],
            )

        gateway = InventoryService._extract_quantastor_gateway_port_host
        self.assertIsNone(gateway({"ipAddress": "[fe80::1%eth0]", "gateway": "192.0.2.1"}))
        self.assertEqual(gateway({"ipAddress": "192.0.2.60", "gateway": "192.0.2.1"}), "192.0.2.60")
        ports = TrueNASRawData(
            enclosures=[],
            disks=[],
            pools=[],
            disk_temperatures={},
            smart_test_results=[],
            cli_network_ports=[
                {"storageSystemId": "node-a", "ipAddress": f"192.0.2.{index}", "gateway": "192.0.2.1"}
                for index in range(100, 120)
            ],
        )
        with self.assertNoLogs("app.services.inventory", level="WARNING"):
            by_system = InventoryService._quantastor_gateway_hosts_by_system(ports)
        self.assertEqual(by_system["node-a"], [f"192.0.2.{index}" for index in range(100, 120)])

    async def test_appliance_reported_hosts_never_reach_fabric_warnings_unadmitted(self) -> None:
        service = _sg_service(self.directory, "quantastor")
        raw = TrueNASRawData(
            enclosures=[],
            disks=[],
            pools=[],
            disk_temperatures={},
            smart_test_results=[],
            systems=[
                {"id": "node-a", "storageSystemClusterId": "cluster-1", "hostname": f"bad host {VALUE_MARKER}"},
                {"id": "node-b", "storageSystemClusterId": "cluster-1", "ipAddress": "192.0.2.51"},
            ],
        )
        contacted: list[str] = []

        async def refused(commands, host, *, stdin_data=None):
            del stdin_data
            contacted.append(host)
            return _refused_probe_results(commands)

        service._run_ssh_commands = AsyncMock(side_effect=refused)
        _overlay, failures = await service._fetch_quantastor_cli_overlay(raw)
        self.assertIn("192.0.2.51", contacted)
        self.assertNotIn(VALUE_MARKER.lower(), json.dumps([contacted, failures]).lower())
        self.assertIn(
            "Quantastor SSH CLI enrichment failed on 192.0.2.51 (connection_refused; exit 255). "
            "REST data is still being used.",
            failures,
        )


class LocalFailureWordingTests(unittest.IsolatedAsyncioTestCase):
    """Local failures keep this service's own fixed wording wherever they are rendered."""

    def setUp(self) -> None:
        for limiter in ssh_probe_module.SSH_LOG_LIMITERS:
            limiter.reset()
        self.addCleanup(lambda: [limiter.reset() for limiter in ssh_probe_module.SSH_LOG_LIMITERS])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def _service(self) -> InventoryService:
        settings = Settings()
        system = SystemConfig(
            id="synthetic-quantastor",
            truenas=TrueNASConfig(platform="quantastor", api_user="synthetic", api_password="synthetic"),
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

    async def test_destination_refusal_keeps_its_fixed_sentence_in_rendered_warnings(self) -> None:
        service = self._service()
        service._build_configured_quantastor_hosts = MagicMock(return_value=["unapproved.example.test"])
        with (
            patch.object(SSHProbe, "run_commands", new=AsyncMock(side_effect=AssertionError("no transport"))),
            patch.object(SSHProbe, "run_planned_commands", new=AsyncMock(side_effect=AssertionError("no transport"))),
            self.assertLogs("app.services.inventory", level="WARNING"),
        ):
            _overlay, cli_failures = await service._fetch_quantastor_cli_overlay()
            _devices, _ses, ses_failures = await service._discover_and_fetch_sg_ses_host_overlay(
                "unapproved.example.test", failure_prefix="Quantastor SSH SES"
            )
        self.assertEqual(
            cli_failures,
            [
                "Quantastor SSH CLI enrichment failed on unapproved.example.test "
                f"(destination_not_approved; exit 255). {REFUSAL_SENTENCE} REST data is still being used."
            ],
        )
        self.assertEqual(
            ses_failures,
            [
                "Quantastor SSH SES discovery failed on unapproved.example.test "
                f"(destination_not_approved; exit 255). {REFUSAL_SENTENCE}"
            ],
        )

        refusals = service._ssh_destination_refusal_results(["first", "second"], "unapproved.example.test")
        for result in refusals:
            # The sentence comes from the local reason, never from result text.
            result.stderr = f"{VALUE_MARKER} rewritten"
        warnings, status = InventoryService._summarize_ssh_failures(refusals, {})
        self.assertEqual(
            warnings,
            [
                f"Could not log in over SSH (destination_not_approved). {REFUSAL_SENTENCE} "
                "Bay details from SSH are missing for this refresh."
            ],
        )
        self.assertEqual(status, "SSH login failed (destination_not_approved).")
        self.assertNotIn(VALUE_MARKER, json.dumps([warnings, status]))

        with self.subTest(control="remote_text_is_not_a_local_sentence"):
            remote = [
                SSHCommandResult(command="first", ok=False, stderr=REFUSAL_SENTENCE, exit_code=255),
                SSHCommandResult(command="second", ok=False, stderr=REFUSAL_SENTENCE, exit_code=255),
            ]
            warnings, _status = InventoryService._summarize_ssh_failures(remote, {})
            self.assertNotIn(REFUSAL_SENTENCE, json.dumps(warnings))
            self.assertEqual(
                service._ssh_failure_public_summary(remote[0]), "ssh_command_failed; exit 255"
            )

    def test_internal_error_keeps_local_wording_in_partial_batches(self) -> None:
        def internal(command: str) -> SSHCommandResult:
            return SSHCommandResult(
                command=command,
                ok=False,
                stderr=VALUE_MARKER,
                exit_code=255,
                local_failure=SSHLocalFailure("internal_error", "RuntimeError"),
            )

        cases = {
            "one_success_one_internal": (
                [SSHCommandResult(command="synthetic-ok", ok=True, stdout="ok", exit_code=0), internal("synthetic-bug")],
                {"synthetic-ok": "ok"},
                [
                    "SSH collection stopped on an internal error (SSH command 2; internal_error). "
                    "Bay details from that read are missing for this refresh."
                ],
                "SSH collection error (internal_error). Some SSH reads are missing for this refresh.",
            ),
            "remote_failure_and_internal": (
                [
                    SSHCommandResult(command="synthetic-ok", ok=True, stdout="ok", exit_code=0),
                    SSHCommandResult(command="synthetic-remote", ok=False, stderr="failure", exit_code=3),
                    internal("synthetic-bug"),
                ],
                {"synthetic-ok": "ok"},
                [
                    "SSH command failed (SSH command 2; ssh_command_failed; exit 3).",
                    "SSH collection stopped on an internal error (SSH command 3; internal_error). "
                    "Bay details from that read are missing for this refresh.",
                ],
                "SSH collection error (internal_error). Some SSH reads are missing for this refresh.",
            ),
            "optional_read_internal": (
                [
                    SSHCommandResult(command="synthetic-ok", ok=True, stdout="ok", exit_code=0),
                    internal("sudo -n nvme list-subsys -o json"),
                ],
                {"synthetic-ok": "ok"},
                [
                    "SSH collection stopped on an internal error in optional reads "
                    "(Linux NVMe subsystem detail; internal_error). The bay map still works; "
                    "the service log has the details."
                ],
                "SSH collection error in optional reads (Linux NVMe subsystem detail; internal_error). "
                "The bay map is complete.",
            ),
        }
        for name, (results, outputs, expected_warnings, expected_status) in cases.items():
            with self.subTest(case=name):
                warnings, status = InventoryService._summarize_ssh_failures(results, outputs)
                self.assertEqual(warnings, expected_warnings)
                self.assertEqual(status, expected_status)
                text = json.dumps([warnings, status])
                self.assertNotIn(VALUE_MARKER, text)
                self.assertNotIn("appliance", text.lower())

        with self.subTest(control="remote_only_partial_batch"):
            warnings, status = InventoryService._summarize_ssh_failures(
                [
                    SSHCommandResult(command="synthetic-ok", ok=True, stdout="ok", exit_code=0),
                    SSHCommandResult(command="synthetic-remote", ok=False, stderr="failure", exit_code=3),
                ],
                {"synthetic-ok": "ok"},
            )
            self.assertEqual(warnings, ["SSH command failed (SSH command 2; ssh_command_failed; exit 3)."])
            self.assertEqual(status, "Some SSH commands failed.")

    async def test_internal_error_in_an_overlay_read_uses_the_local_sentence(self) -> None:
        service = self._service()
        service._build_configured_quantastor_hosts = MagicMock(return_value=["node.example.test"])

        async def results(commands, _host, *, stdin_data=None):
            del stdin_data
            return [
                SSHCommandResult(
                    command=command,
                    ok=False,
                    stderr=VALUE_MARKER,
                    exit_code=255,
                    local_failure=SSHLocalFailure("internal_error", "RuntimeError"),
                )
                if index == 0
                else SSHCommandResult(command=command, ok=True, stdout="[]", exit_code=0)
                for index, command in enumerate(commands)
            ]

        service._run_ssh_commands = AsyncMock(side_effect=results)
        _overlay, failures = await service._fetch_quantastor_cli_overlay()
        self.assertIn(
            f"Quantastor SSH CLI disk inventory failed on node.example.test (internal_error; exit 255). "
            f"{INTERNAL_SENTENCE}",
            failures,
        )
        self.assertNotIn(VALUE_MARKER, json.dumps(failures))


class InternalErrorFrameRecordTests(unittest.TestCase):
    """The internal-error record carries frame facts only: base name, line, function."""

    def test_internal_error_record_has_no_paths_source_or_message(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        hidden_directory = "SYNTHETIC-DIRECTORY-MARKER"
        injected_path = str(Path(directory.name) / hidden_directory / "injected_module.py")
        namespace: dict[str, Any] = {}
        source = (
            "def descend(depth, exc):\n"
            "    if depth <= 0:\n"
            "        raise exc  # SYNTHETIC-SOURCE-TEXT\n"
            "    descend(depth - 1, exc)\n"
        )
        exec(compile(source, injected_path, "exec"), namespace)  # noqa: S102 - local synthetic frames
        descend = namespace["descend"]

        def connect(self, **_kwargs):
            descend(40, RuntimeError(VALUE_MARKER))

        probe = SSHProbe(
            SSHConfig(enabled=True, host="node.example.test", user="synthetic-operator", key_path="", known_hosts_path="")
        )
        with (
            patch.object(paramiko.SSHClient, "connect", connect),
            self.assertLogs("app.services.ssh_probe", level="ERROR") as logs,
        ):
            probe._run_commands_sync(["synthetic"])

        self.assertEqual(len(logs.records), 1, logs.output)
        record = logs.records[0]
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)
        rendered = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(pathname)s %(message)s"
        ).format(record)
        message = record.getMessage()
        header, _separator, stack = message.partition("\nStack (exception message omitted):\n")
        self.assertIn("reason=internal_error type=RuntimeError", header)
        frame_lines = stack.splitlines()
        self.assertTrue(frame_lines)
        self.assertLessEqual(len(frame_lines), 20)
        for line in frame_lines:
            self.assertRegex(line, FRAME_LINE_RE)
        self.assertEqual(frame_lines[-1], "  injected_module.py:3 in descend")
        roots = {
            str(Path(__file__).resolve().parents[1]),
            sys.prefix,
            sys.base_prefix,
            str(Path.home()),
            tempfile.gettempdir(),
            directory.name,
        }
        for forbidden in (VALUE_MARKER, hidden_directory, "SYNTHETIC-SOURCE-TEXT", "raise exc", *roots):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, message)
        # The logging module's own pathname field names the logging call site,
        # which is this service's module, not a frame of the failure.
        self.assertNotIn(hidden_directory, rendered)
        self.assertNotIn(VALUE_MARKER, rendered)

    def test_frame_formatter_bounds_names_and_count(self) -> None:
        def raise_from(code_name: str, filename: str):
            namespace: dict[str, Any] = {}
            exec(compile("def f():\n    raise ValueError('x')\n", filename, "exec"), namespace)  # noqa: S102
            function = namespace["f"]
            function.__code__ = function.__code__.replace(co_name=code_name)
            try:
                function()
            except ValueError as exc:
                return exc
            raise AssertionError("no exception")

        exc = raise_from("bad name\nwith newline", "/synthetic root/" + "n" * 200 + ".py")
        formatter = getattr(ssh_probe_module, "format_value_safe_frames", None)
        self.assertIsNotNone(formatter, "the probe module has no value-safe frame formatter")
        text = formatter(exc.__traceback__)
        lines = text.splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, FRAME_LINE_RE)
        self.assertNotIn("synthetic root", text)
        self.assertNotIn("\nwith newline", text)
        self.assertEqual(lines[-1], "  <file>:2 in <function>")


def _sg_discovery_stdout(indexes: Iterable[int]) -> str:
    # The shell glob lists device nodes in string order (sg1, sg10, sg100, sg2).
    return "".join(f"{path}\n" for path in sorted(f"/dev/sg{index}" for index in indexes))


def _quantastor_snapshot_service(directory: str, node_a_row: dict[str, Any]) -> tuple[InventoryService, list[str]]:
    service = _sg_service(directory, "quantastor")
    service.truenas_client.fetch_all.return_value = TrueNASRawData(
        enclosures=[],
        disks=[],
        pools=[],
        disk_temperatures={},
        smart_test_results=[],
        systems=[
            {"id": "node-a", "storageSystemClusterId": "cluster-1", **node_a_row},
            {"id": "node-b", "storageSystemClusterId": "cluster-1", "ipAddress": "192.0.2.51"},
        ],
    )
    contacted: list[str] = []

    async def refused(commands, host, *, stdin_data=None):
        del stdin_data
        contacted.append(host)
        return _refused_probe_results(commands)

    async def planned(planner, *, initial_commands, host):
        del planner
        contacted.append(host)
        return _refused_probe_results(list(initial_commands))

    service._run_ssh_commands = AsyncMock(side_effect=refused)
    service._run_ssh_planned_commands = planned
    return service, contacted


def _answer_network_port_list(service: InventoryService, contacted: list[str], ports: list[dict[str, Any]]) -> None:
    """The node's CLI answers the network port list; every other SSH read is refused."""

    async def answer(commands, host, *, stdin_data=None):
        del stdin_data
        contacted.append(host)
        results = _refused_probe_results(commands)
        return [
            SSHCommandResult(command=command, ok=True, stdout=json.dumps(ports), exit_code=0)
            if "network-port-list" in command
            else result
            for command, result in zip(commands, results)
        ]

    service._run_ssh_commands = AsyncMock(side_effect=answer)


def _documentation_addresses(count: int) -> list[str]:
    pool = [f"198.51.100.{index}" for index in range(1, 201)] + [f"203.0.113.{index}" for index in range(1, 201)]
    return pool[:count]


REPORTED_ADDRESS_NOT_USABLE_ONE = (
    "QuantaStor reported 1 node address that is not an IP address or DNS name "
    "(reported address not usable). It was not tried over SSH."
)


class DiscoveryScaleAndReportedHostTests(unittest.IsolatedAsyncioTestCase):
    """Real hardware counts are read in full; a reported host is read by a total parser."""

    def setUp(self) -> None:
        for limiter in ssh_probe_module.SSH_LOG_LIMITERS:
            limiter.reset()
        self.addCleanup(lambda: [limiter.reset() for limiter in ssh_probe_module.SSH_LOG_LIMITERS])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    @staticmethod
    def _planned_with_quiet_pages(service: InventoryService, discovery_stdout: str, planned: list[str]) -> None:
        async def run_planned(planner, *, initial_commands, host):
            del host
            discovery = SSHCommandResult(command=initial_commands[0], ok=True, stdout=discovery_stdout, exit_code=0)
            commands = planner([discovery])
            planned.extend(commands)
            return [discovery, *(SSHCommandResult(command=command, ok=True, stdout="", exit_code=0) for command in commands)]

        service._run_ssh_planned_commands = run_planned

    async def test_sg_discovery_reads_large_real_shelf_counts_without_a_warning(self) -> None:
        for count in (70, 1024, 5200):
            with self.subTest(devices=count):
                service = _sg_service(self.directory)
                planned: list[str] = []
                self._planned_with_quiet_pages(service, _sg_discovery_stdout(range(count)), planned)
                with self.assertNoLogs("app.services.inventory", level="WARNING"):
                    devices, _overlay, failures = await service._discover_and_fetch_sg_ses_host_overlay(
                        "node.example.test", failure_prefix="TrueNAS SCALE SSH SES"
                    )
                self.assertEqual(devices, [f"/dev/sg{index}" for index in range(count)])
                self.assertEqual(failures, [])
                self.assertEqual(
                    len({shlex.split(command)[-1] for command in planned if "sg_ses" in command}), count
                )

    async def test_sg_discovery_limit_is_shown_on_the_page_with_both_counts(self) -> None:
        service = _sg_service(self.directory)
        planned: list[str] = []
        self._planned_with_quiet_pages(service, _sg_discovery_stdout(range(16385)), planned)
        with self.assertLogs("app.services.inventory", level="WARNING") as logs:
            snapshot = await service.get_sas_fabric_snapshot(force_refresh=True)
        expected = (
            "TrueNAS SCALE SSH SES read is incomplete on node.example.test: 16384 SG devices read, "
            "1 left out at the limit of 16384 per host. Bays behind the devices left out are missing "
            "for this refresh."
        )
        encoded = json.loads(json.dumps(snapshot.model_dump(mode="json")))
        self.assertIn(expected, encoded["warnings"])
        planned_devices = {shlex.split(command)[-1] for command in planned if "sg_ses" in command}
        self.assertEqual(len(planned_devices), 16384)
        self.assertIn("/dev/sg16383", planned_devices)
        self.assertNotIn("/dev/sg16384", planned_devices)
        counted = [line for line in logs.output if "operation=sg_ses_discovery" in line]
        self.assertEqual(len(counted), 1, logs.output)
        self.assertIn("1 devices beyond the limit of 16384", counted[0])

    async def test_sg_devices_are_ordered_by_numeric_index_before_any_cut(self) -> None:
        stdout = "/dev/sg100\n/dev/sg10\n/dev/sg1\n/dev/sg2\n"
        devices, rejected, beyond = InventoryService._admit_sg_ses_devices(stdout.splitlines())
        self.assertEqual((devices, rejected, beyond), (["/dev/sg1", "/dev/sg2", "/dev/sg10", "/dev/sg100"], 0, 0))

        service = _sg_service(self.directory)
        self._planned_with_quiet_pages(service, stdout, [])
        with (
            patch.object(inventory_module, "SG_SES_MAX_DISCOVERED_DEVICES", 2),
            patch.object(InventoryService, "_score_sg_ses_overlay", return_value=1),
        ):
            self.assertEqual(
                InventoryService._admit_sg_ses_devices(stdout.splitlines()), (["/dev/sg1", "/dev/sg2"], 0, 2)
            )
            for refresh in (1, 2):
                with self.subTest(refresh=refresh):
                    _overlay, failures, _best = await service._fetch_sg_ses_overlay(
                        ["node.example.test"], failure_prefix="TrueNAS SCALE SSH SES"
                    )
                    self.assertIn(
                        "TrueNAS SCALE SSH SES read is incomplete on node.example.test: 2 SG devices read, "
                        "2 left out at the limit of 2 per host. Bays behind the devices left out are missing "
                        "for this refresh.",
                        failures,
                    )
                    # A cut device list is not cached, so the next refresh still says so.
                    self.assertEqual(service._get_cached_sg_ses_devices("node.example.test"), [])

        with self.subTest(control="within_limit_is_cached"):
            service = _sg_service(self.directory)
            self._planned_with_quiet_pages(service, stdout, [])
            with patch.object(InventoryService, "_score_sg_ses_overlay", return_value=1):
                _overlay, failures, _best = await service._fetch_sg_ses_overlay(
                    ["node.example.test"], failure_prefix="TrueNAS SCALE SSH SES"
                )
            self.assertEqual(failures, [])
            self.assertEqual(
                service._get_cached_sg_ses_devices("node.example.test"),
                ["/dev/sg1", "/dev/sg2", "/dev/sg10", "/dev/sg100"],
            )

    async def test_sg_incomplete_read_on_any_host_stays_on_the_page(self) -> None:
        service = _sg_service(self.directory)
        answers = {"node-a.example.test": "/dev/sg1\n/dev/sg2\n/dev/sg3\n", "node-b.example.test": "/dev/sg1\n"}

        async def run_planned(planner, *, initial_commands, host):
            discovery = SSHCommandResult(command=initial_commands[0], ok=True, stdout=answers[host], exit_code=0)
            commands = planner([discovery])
            return [discovery, *(SSHCommandResult(command=command, ok=True, stdout="", exit_code=0) for command in commands)]

        service._run_ssh_planned_commands = run_planned
        with (
            patch.object(inventory_module, "SG_SES_MAX_DISCOVERED_DEVICES", 2),
            # The cut host scores lower, so the other host's overlay is the one kept.
            patch.object(InventoryService, "_score_sg_ses_overlay", side_effect=[1, 2]),
        ):
            _overlay, failures, best = await service._fetch_sg_ses_overlay(
                list(answers), failure_prefix="TrueNAS SCALE SSH SES"
            )
        self.assertEqual(best, "node-b.example.test")
        self.assertEqual(
            failures,
            [
                "TrueNAS SCALE SSH SES read is incomplete on node-a.example.test: 2 SG devices read, "
                "1 left out at the limit of 2 per host. Bays behind the devices left out are missing "
                "for this refresh."
            ],
        )

    def test_sg_device_digits_are_ascii_only_in_every_position(self) -> None:
        values = ["/dev/sg1\u0663", "/dev/sg\u06631", "/dev/sg1\uff12", "/dev/sg7"]
        self.assertEqual(InventoryService._admit_sg_ses_devices(values), (["/dev/sg7"], 3, 0))

    async def test_reported_host_reader_is_total_and_never_rewrites_an_address(self) -> None:
        label = "a" * 63
        name_253 = ".".join([label, label, label, "b" * 61])
        cases: dict[str, str | None] = {
            "[::1": None,
            "::1]": None,
            "[bad]": None,
            "\uff3b2001:db8::1\uff3d": None,
            "node\uff1a22.example.test": None,
            "node\uff0fx.example.test": None,
            "fe80::1%eth0": None,
            "[fe80::1%eth0]": None,
            "bad host": None,
            "node-e\n.example.test": None,
            "node-f\t.example.test": None,
            "operator@node.example.test": None,
            "node.example.test:22": None,
            "ssh://node.example.test": None,
            "-oProxyCommand=x": None,
            "b" * 64 + ".example.test": None,
            name_253 + "c": None,
            "2001:db8::1": "2001:db8::1",
            "2001:DB8::A": "2001:db8::a",
            "::ffff:192.0.2.70": "::ffff:192.0.2.70",
            "[::ffff:192.0.2.71]": "::ffff:192.0.2.71",
            "[2001:db8::2]": "2001:db8::2",
            "node_b.example.test": "node_b.example.test",
            "NODE-C.Example.TEST": "node-c.example.test",
            "node-d.example.test.": "node-d.example.test.",
            "nodee": "nodee",
            "123": "123",
            label + ".example.test": label + ".example.test",
            name_253: name_253,
            "192.0.2.61": "192.0.2.61",
            "[192.0.2.62]": None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                service, contacted = _quantastor_snapshot_service(self.directory, {"hostname": value})
                try:
                    snapshot = await service.get_sas_fabric_snapshot(force_refresh=True)
                except Exception as exc:  # noqa: BLE001 - the reader must be total
                    self.fail(f"the reported-host reader raised {type(exc).__name__}")
                encoded = json.loads(json.dumps(snapshot.model_dump(mode="json")))
                warnings = encoded["warnings"]
                self.assertLessEqual(
                    set(contacted), {expected, "192.0.2.51", "node.example.test"} - {None}, contacted
                )
                if expected is None:
                    self.assertIn(REPORTED_ADDRESS_NOT_USABLE_ONE, warnings)
                    self.assertNotIn(value.strip(), json.dumps(warnings))
                else:
                    self.assertIn(expected, contacted)
                    self.assertNotIn(REPORTED_ADDRESS_NOT_USABLE_ONE, warnings)
                    if expected != value:
                        self.assertNotIn(value, contacted)

    def test_host_identity_comparison_never_raises_and_keeps_ipv6_whole(self) -> None:
        same = InventoryService._quantastor_hosts_match
        self.assertFalse(same("2001:db8::1", "2001:db8::2"))
        self.assertTrue(same("2001:db8::1", "https://[2001:db8::1]:8153"))
        self.assertTrue(same("Node-A.Example.Test", "node-a.example.test"))
        for value in ("[::1", "::1]", "[bad]", "\uff3b2001:db8::1\uff3d", "node\uff1a22.example.test"):
            with self.subTest(value=value):
                try:
                    matched = same(value, "192.0.2.51")
                except ValueError:
                    self.fail("host identity comparison raised")
                self.assertFalse(matched)

    async def test_reported_host_limit_is_shown_on_the_page_with_both_counts(self) -> None:
        service, contacted = _quantastor_snapshot_service(
            self.directory, {"ipAddresses": _documentation_addresses(257)}
        )
        snapshot = await service.get_sas_fabric_snapshot(force_refresh=True)
        self.assertIn(
            "QuantaStor node address read is incomplete: 256 addresses used, 1 left out at the limit of "
            "256 per reported address list. The addresses left out were not tried over SSH.",
            snapshot.warnings,
        )
        self.assertIn("203.0.113.56", contacted)
        self.assertNotIn("203.0.113.57", contacted)

        for count in (8, 9, 256):
            with self.subTest(control=count):
                addresses = _documentation_addresses(count)
                service, contacted = _quantastor_snapshot_service(self.directory, {"ipAddresses": addresses})
                snapshot = await service.get_sas_fabric_snapshot(force_refresh=True)
                self.assertFalse([w for w in snapshot.warnings if "node address" in w], snapshot.warnings)
                self.assertLessEqual(set(addresses), set(contacted))

    async def test_address_limit_applies_per_reported_address_list(self) -> None:
        service, contacted = _quantastor_snapshot_service(
            self.directory, {"ipAddresses": _documentation_addresses(257)}
        )
        _answer_network_port_list(
            service,
            contacted,
            [
                {"storageSystemId": "node-a", "ipAddress": f"2001:db8::{index:x}", "gateway": "2001:db8::fffe"}
                for index in range(1, 258)
            ],
        )
        snapshot = await service.get_sas_fabric_snapshot(force_refresh=True)
        encoded = json.loads(json.dumps(snapshot.model_dump(mode="json")))
        self.assertEqual(
            [warning for warning in encoded["warnings"] if "node address read is incomplete" in warning],
            [
                "QuantaStor node address read is incomplete: 512 addresses used, 2 left out at the limit of "
                "256 per reported address list. The addresses left out were not tried over SSH."
            ],
        )

    async def test_reported_address_is_validated_as_reported_at_both_ends(self) -> None:
        controls = [chr(code) for code in range(0x20)] + ["\x7f"]
        sources = {"system_row": "node-a.example.test", "gateway_port": "192.0.2.70"}

        async def read(source: str, value: str) -> tuple[list[str], list[str]]:
            row = {"hostname": value} if source == "system_row" else {}
            service, contacted = _quantastor_snapshot_service(self.directory, row)
            if source == "gateway_port":
                _answer_network_port_list(
                    service, contacted, [{"storageSystemId": "node-a", "ipAddress": value, "gateway": "192.0.2.1"}]
                )
            snapshot = await service.get_sas_fabric_snapshot(force_refresh=True)
            return json.loads(json.dumps(snapshot.model_dump(mode="json")))["warnings"], contacted

        for source, host in sources.items():
            for value in (host, f"  {host}", f"{host}  ", f" {host} "):
                with self.subTest(source=source, control=repr(value)):
                    warnings, contacted = await read(source, value)
                    self.assertIn(host, contacted)
                    self.assertNotIn(REPORTED_ADDRESS_NOT_USABLE_ONE, warnings)
            for character in controls:
                for value in (character + host, host + character, f" {character}{host}", f"{host}{character} "):
                    with self.subTest(source=source, value=repr(value)):
                        warnings, contacted = await read(source, value)
                        self.assertIn(REPORTED_ADDRESS_NOT_USABLE_ONE, warnings)
                        self.assertNotIn(host, contacted)

    def test_host_identity_is_none_for_values_that_are_not_text(self) -> None:
        identity = InventoryService._normalize_host_identity
        self.assertEqual(identity("Node-A.Example.Test"), "node-a.example.test")
        self.assertIsNone(identity(None))
        for value in (b"node-a.example.test", bytearray(b"node-a.example.test"), memoryview(b"node-a.example.test"), 7):
            with self.subTest(value_type=type(value).__name__):
                try:
                    result = identity(value)
                    matched = InventoryService._quantastor_hosts_match(value, "node-a.example.test")
                except Exception as exc:  # noqa: BLE001 - host identity must be total
                    self.fail(f"host identity raised {type(exc).__name__}")
                self.assertIsNone(result)
                self.assertFalse(matched)

    def test_gateway_port_hosts_are_read_far_above_real_port_counts(self) -> None:
        ports = [
            {"storageSystemId": "node-a", "ipAddress": f"198.51.100.{index}", "gateway": "198.51.100.254"}
            for index in range(1, 201)
        ] + [
            {"storageSystemId": "node-a", "ipAddress": f"2001:db8::{index:x}", "gateway": "2001:db8::fffe"}
            for index in range(1, 58)
        ] + [{"storageSystemId": "node-a", "ipAddress": "fe80::1%eth0", "gateway": "192.0.2.1"}]
        raw = TrueNASRawData(
            enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[], cli_network_ports=ports
        )
        by_system = InventoryService._quantastor_gateway_hosts_by_system(raw)
        self.assertEqual(len(by_system["node-a"]), 256)
        self.assertEqual(by_system["node-a"][-1], "2001:db8::38")
        self.assertEqual(
            InventoryService._quantastor_reported_host_warnings(raw),
            [
                REPORTED_ADDRESS_NOT_USABLE_ONE,
                "QuantaStor node address read is incomplete: 256 addresses used, 1 left out at the limit of "
                "256 per reported address list. The addresses left out were not tried over SSH.",
            ],
        )
        healthy = TrueNASRawData(
            enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[], cli_network_ports=ports[:12]
        )
        self.assertEqual(len(InventoryService._quantastor_gateway_hosts_by_system(healthy)["node-a"]), 12)
        self.assertEqual(InventoryService._quantastor_reported_host_warnings(healthy), [])

    def test_admission_warnings_use_a_fixed_limiter_state(self) -> None:
        now = [1000.0]
        limiter = RepeatedLogLimiter(repeat_interval_seconds=60, clock=lambda: now[0])
        service = _sg_service(self.directory)
        with (
            patch.object(ssh_probe_module, "SSH_COMMAND_FAILURE_LOG", limiter),
            self.assertLogs("app.services.inventory", level="WARNING") as logs,
        ):
            for count in (1, 2, 1, 2):
                service._log_sg_ses_discovery_admission("node.example.test", count, 0)
                InventoryService._log_reported_host_admission(count, 0)
            now[0] += 61
            service._log_sg_ses_discovery_admission("node.example.test", 2, 3)
            InventoryService._log_reported_host_admission(2, 3)
        sg_lines = [line for line in logs.output if "operation=sg_ses_discovery" in line]
        host_lines = [line for line in logs.output if "operation=quantastor_node_hosts" in line]
        self.assertEqual(len(sg_lines), 2, logs.output)
        self.assertEqual(len(host_lines), 2, logs.output)
        self.assertIn("ignored 1 lines", sg_lines[0])
        self.assertIn("ignored 1 values", host_lines[0])
        self.assertIn("ignored 2 lines that are not an SG device path and 3 devices", sg_lines[1])
        self.assertIn("ignored 2 values that are not an IP address or DNS name and 3 beyond", host_lines[1])
        self.assertIn("3 identical messages suppressed", sg_lines[1])


if __name__ == "__main__":
    unittest.main()
