from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, patch

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
                    "Could not log in over SSH (ssh_transport_failed). "
                    "Bay details from SSH are missing for this refresh."
                ],
                "SSH login failed (ssh_transport_failed).",
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

    def test_log_redaction_covers_auth_headers_cookies_and_userinfo(self) -> None:
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
                return_value=[
                    SSHCommandResult(
                        command=discovery,
                        ok=False,
                        stderr=f"connection refused: {marker}",
                        exit_code=255,
                    )
                ]
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
                return [
                    SSHCommandResult(
                        command=command,
                        ok=False,
                        stderr=f"connection refused: {marker}",
                        exit_code=255,
                    )
                    for command in commands
                ]

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
                return_value=[
                    SSHCommandResult(
                        command=discovery,
                        ok=False,
                        stderr=f"connection refused: {marker}",
                        exit_code=255,
                    )
                ]
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
        marker = "SYNTHETIC-LOGIN-DETAIL"
        cases = {
            "Authentication failed": "auth_failed",
            "Host key verification failed": "host_key_rejected",
            "Connection refused": "connection_refused",
            "Connection timed out": "timeout",
        }

        for detail, reason in cases.items():
            with self.subTest(reason=reason):
                results = [
                    SSHCommandResult(
                        command=f"synthetic {position}",
                        ok=False,
                        stderr=f"{detail}: {marker}",
                        exit_code=255,
                    )
                    for position in range(2)
                ]
                warnings, status = InventoryService._summarize_ssh_failures(results, {})
                projected = InventoryService._ssh_failure_details(results)
                encoded = json.dumps([warnings, status, projected], sort_keys=True)
                self.assertNotIn(marker, encoded)
                self.assertTrue(all(item["reason"] == reason for item in projected))
                self.assertIn(reason, warnings[0])
                self.assertIn(reason, status)

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


if __name__ == "__main__":
    unittest.main()
