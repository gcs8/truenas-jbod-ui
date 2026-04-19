from __future__ import annotations

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
            self.assertIn("not written to config.yaml", str(result["detail"]))

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
        self.assertIn("jbodmap ALL=(root) NOPASSWD: JBODMAP_CORE_CMDS", content)
        self.assertNotIn("\n+  ", content)

    def test_build_sudoers_content_uses_bootstrap_seed_commands_for_scale(self) -> None:
        content = ServiceAccountBootstrapService._build_sudoers_content(
            "jbodmap",
            "scale",
            [
                "/usr/sbin/zpool status -gP",
                "sudo -n /usr/bin/sg_ses -p aes /dev/sg27",
                "sudo -n /usr/bin/sg_ses -p ec /dev/sg38",
            ],
        )

        self.assertNotIn("/usr/sbin/zpool status -gP", content)
        self.assertIn("/usr/bin/sg_ses -p aes /dev/sg*", content)
        self.assertIn("/usr/bin/sg_ses -p ec /dev/sg*", content)
        self.assertIn("/usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*", content)
        self.assertIn("/usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*", content)
        self.assertIn("/usr/sbin/smartctl -x -j *", content)
        self.assertNotIn("sudo -n", content)


if __name__ == "__main__":
    unittest.main()
