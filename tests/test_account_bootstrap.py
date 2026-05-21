from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from admin_service.services.account_bootstrap import ServiceAccountBootstrapService
from app.models.domain import SystemSetupBootstrapRequest
from app.services.ssh_key_manager import SSHKeyManager
from app.services.ssh_probe import SSHCommandResult


class FakeProbe:
    last_config = None
    last_command = None

    def __init__(self, config) -> None:
        type(self).last_config = config

    def run_command_sync(self, command: str) -> SSHCommandResult:
        type(self).last_command = command
        return SSHCommandResult(
            command=command,
            ok=True,
            stdout=(
                "BOOTSTRAP_SERVICE_USER=jbodmap\n"
                "BOOTSTRAP_SERVICE_HOME=/home/jbodmap\n"
                "BOOTSTRAP_AUTHORIZED_KEYS_PATH=/home/jbodmap/.ssh/authorized_keys\n"
                "BOOTSTRAP_SUDOERS_PATH=/etc/sudoers.d/truenas-jbod-ui-jbodmap\n"
                "BOOTSTRAP_PERMISSION_TARGET=/etc/sudoers.d/truenas-jbod-ui-jbodmap\n"
            ),
            stderr="",
            exit_code=0,
        )


class ServiceAccountBootstrapServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeProbe.last_config = None
        FakeProbe.last_command = None

    def make_service(self, config_file: Path) -> ServiceAccountBootstrapService:
        return ServiceAccountBootstrapService(str(config_file), probe_factory=FakeProbe)

    def write_private_key(self, path: Path) -> None:
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.write_bytes(private_bytes)

    def test_bootstrap_uses_managed_key_without_sudo_for_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config" / "config.yaml"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            key_manager = SSHKeyManager(str(config_file))
            generated_key = key_manager.generate_keypair("id_truenas")
            service = self.make_service(config_file)

            payload = SystemSetupBootstrapRequest(
                platform="core",
                host="nas.example.local",
                bootstrap_user="root",
                bootstrap_password="bootstrap-secret",
                service_user="jbodmap",
                service_key_name=generated_key["name"],
            )

            result = service.bootstrap_service_account(payload)

            self.assertTrue(result["ok"])
            self.assertEqual(result["key_source"], f"managed key {generated_key['name']}")
            self.assertEqual(result["service_user"], "jbodmap")
            self.assertEqual(FakeProbe.last_config.user, "root")
            self.assertEqual(FakeProbe.last_config.password, "bootstrap-secret")
            self.assertTrue(str(FakeProbe.last_command).startswith("/bin/sh -lc "))
            self.assertIn("midclt call user.update", str(FakeProbe.last_command))
            self.assertIn("not written to config.yaml", str(result["detail"]))
            self.assertEqual(result["permission_target"], "/etc/sudoers.d/truenas-jbod-ui-jbodmap")

    def test_bootstrap_uses_sudo_and_private_key_path_for_non_root_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config" / "config.yaml"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            private_key_path = Path(temp_dir) / "manual_runtime_key"
            self.write_private_key(private_key_path)
            service = self.make_service(config_file)

            payload = SystemSetupBootstrapRequest(
                platform="scale",
                host="10.0.0.15",
                bootstrap_user="installer",
                bootstrap_password="installer-secret",
                bootstrap_sudo_password="sudo-secret",
                service_user="jbodmap",
                service_key_path=str(private_key_path),
            )

            result = service.bootstrap_service_account(payload)

            self.assertTrue(result["ok"])
            self.assertEqual(result["key_source"], str(private_key_path))
            self.assertEqual(FakeProbe.last_config.user, "installer")
            self.assertEqual(FakeProbe.last_config.sudo_password, "sudo-secret")
            self.assertTrue(str(FakeProbe.last_command).startswith("sudo -n /bin/sh -lc "))

    def test_build_sudoers_content_formats_wrapped_commands_cleanly(self) -> None:
        content = ServiceAccountBootstrapService._build_sudoers_content("jbodmap", "core")

        self.assertIn("Cmnd_Alias JBODMAP_CORE_CMDS", content)
        self.assertIn("/usr/sbin/sesutil map", content)
        self.assertIn("/usr/sbin/mprutil show adapters", content)
        self.assertIn("/usr/sbin/mprutil -u * show expanders", content)
        self.assertIn("/usr/local/sbin/dmidecode -t slot", content)
        self.assertIn("/usr/bin/tail -n 4000 /var/log/messages", content)
        self.assertIn("jbodmap ALL=(root) NOPASSWD: JBODMAP_CORE_CMDS", content)
        self.assertNotIn("\n+  ", content)
        self.assertNotIn("/usr/sbin/mprutil *", content)

    def test_build_sudoers_content_normalizes_core_mprutil_unit_commands(self) -> None:
        content = ServiceAccountBootstrapService._build_sudoers_content(
            "jbodmap",
            "core",
            [
                "sudo -n /usr/sbin/mprutil -u 1 show expanders",
                "sudo -n /usr/sbin/mprutil -u 0 show iocfacts",
            ],
        )

        self.assertIn("/usr/sbin/mprutil -u * show expanders", content)
        self.assertIn("/usr/sbin/mprutil -u * show iocfacts", content)
        self.assertIn("/usr/sbin/mprutil show adapters", content)
        self.assertNotIn("/usr/sbin/mprutil -u 1 show expanders", content)
        self.assertNotIn("/usr/sbin/mprutil *", content)

    def test_build_sudoers_content_normalizes_core_dmidecode_slot_command(self) -> None:
        content = ServiceAccountBootstrapService._build_sudoers_content(
            "jbodmap",
            "core",
            [
                "sudo -n /usr/local/sbin/dmidecode -t slot 2>/dev/null || true",
            ],
        )

        self.assertIn("/usr/local/sbin/dmidecode -t slot", content)
        self.assertNotIn("2>/dev/null", content)
        self.assertNotIn("|| true", content)

    def test_build_sudoers_content_normalizes_core_messages_tail_command(self) -> None:
        content = ServiceAccountBootstrapService._build_sudoers_content(
            "jbodmap",
            "core",
            [
                "sudo -n /usr/bin/tail -n 4000 /var/log/messages 2>/dev/null || true",
            ],
        )

        self.assertIn("/usr/bin/tail -n 4000 /var/log/messages", content)
        self.assertNotIn("2>/dev/null", content)
        self.assertNotIn("|| true", content)

    def test_build_core_midclt_command_uses_same_normalized_commands(self) -> None:
        command = ServiceAccountBootstrapService._build_core_midclt_user_update_command(
            "USER_ID",
            requested_commands=[
                "sudo -n /usr/sbin/sesutil show",
                "sudo -n /usr/sbin/mprutil -u 1 show expanders",
            ],
        )
        tokens = shlex.split(command)
        payload = json.loads(tokens[-1])

        self.assertEqual(tokens[:4], ["midclt", "call", "user.update", "USER_ID"])
        self.assertTrue(payload["sudo"])
        self.assertTrue(payload["sudo_nopasswd"])
        self.assertIn("/usr/sbin/mprutil -u * show expanders", payload["sudo_commands"])
        self.assertIn("/usr/sbin/mprutil show adapters", payload["sudo_commands"])
        self.assertIn("/usr/local/sbin/dmidecode -t slot", payload["sudo_commands"])
        self.assertIn("/usr/bin/tail -n 4000 /var/log/messages", payload["sudo_commands"])
        self.assertNotIn("/usr/sbin/mprutil -u 1 show expanders", payload["sudo_commands"])

    def test_build_sudoers_content_uses_bootstrap_seed_commands_for_scale(self) -> None:
        content = ServiceAccountBootstrapService._build_sudoers_content(
            "jbodmap",
            "scale",
            [
                "/usr/sbin/zpool status -gP",
                "sudo -n /usr/bin/sg_ses -p aes /dev/sg26",
                "sudo -n /usr/bin/sg_ses -p ec /dev/sg37",
                "sudo -n /usr/bin/sg_ses --join --filter /dev/sg26",
            ],
        )

        self.assertNotIn("/usr/sbin/zpool status -gP", content)
        self.assertIn("/usr/bin/sg_ses -p aes /dev/sg*", content)
        self.assertIn("/usr/bin/sg_ses -p ec /dev/sg*", content)
        self.assertIn("/usr/bin/sg_ses --join --filter /dev/sg*", content)
        self.assertIn("/usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*", content)
        self.assertIn("/usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*", content)
        self.assertIn("/usr/sbin/smartctl -x -j *", content)
        self.assertNotIn("sudo -n", content)


if __name__ == "__main__":
    unittest.main()
