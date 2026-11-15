from __future__ import annotations

import json
import unittest

from app.models.domain import SasFabricSnapshot
from app.services.inventory import InventoryService
from app.services.ssh_probe import SSHCommandResult, redact_ssh_command


class SSHDiagnosticSafetyTests(unittest.TestCase):
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
                    "context": "ssh_command",
                    "context_label": "SSH command",
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
                ["Could not log in over SSH. Bay details from SSH are missing for this refresh."],
                "SSH login failed.",
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
                ["SSH command failed (SSH command; exit 23)."],
                "Some SSH commands failed.",
            ),
        }

        for name, (results, outputs, expected_warnings, expected_status) in cases.items():
            with self.subTest(name=name):
                warnings, status = InventoryService._summarize_ssh_failures(results, outputs)
                self.assertEqual(warnings, expected_warnings)
                self.assertEqual(status, expected_status)
                self.assertNotIn(private_marker, json.dumps([warnings, status]))

    def test_log_redaction_covers_auth_headers_cookies_userinfo_and_schemes(self) -> None:
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
            "bare_scheme": (
                f"client Bearer {private_marker}",
                "Bearer '***'",
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


if __name__ == "__main__":
    unittest.main()
