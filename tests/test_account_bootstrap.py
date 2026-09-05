from __future__ import annotations

import fnmatch
import json
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from admin_service.services.account_bootstrap import (
    SUDO_COMMANDS_BY_PLATFORM,
    ServiceAccountBootstrapService,
    saved_sudo_commands_for_system,
)
from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import LedAction, SystemSetupBootstrapRequest
from app.services.inventory import (
    LINUX_BOOT_MEDIA_SMARTCTL_DEVICE_TYPE,
    InventoryService,
)
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.ssh_key_manager import SSHKeyManager
from app.services.ssh_probe import SSHCommandResult
from app.services.system_setup import default_ssh_commands_for_platform


Platform = Literal["core", "scale", "quantastor", "linux"]


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

    def test_generated_private_key_is_readable_by_the_app_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config" / "config.yaml"
            config_file.parent.mkdir(parents=True, exist_ok=True)

            generated_key = SSHKeyManager(str(config_file)).generate_keypair("id_truenas")

            private_path = Path(generated_key["private_path"])
            self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o640)

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

    def test_bootstrap_derives_known_hosts_path_instead_of_using_request_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config" / "config.yaml"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            key_manager = SSHKeyManager(str(config_file))
            generated_key = key_manager.generate_keypair("id_truenas")
            service = self.make_service(config_file)

            result = service.bootstrap_service_account(
                SystemSetupBootstrapRequest(
                    platform="core",
                    host="nas.example.local",
                    bootstrap_user="root",
                    bootstrap_password="bootstrap-secret",
                    bootstrap_known_hosts_path=str(Path(temp_dir) / "request-selected-known-hosts"),
                    bootstrap_strict_host_key_checking=False,
                    service_user="jbodmap",
                    service_key_name=generated_key["name"],
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                FakeProbe.last_config.known_hosts_path,
                str(Path(temp_dir) / "data" / "known_hosts"),
            )

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

    def test_saved_sudo_commands_for_system_returns_only_saved_sudo_lines(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-scale",
                    ssh=SSHConfig(
                        enabled=True,
                        host="saved.example.test",
                        user="jbodmap",
                        commands=[
                            "/usr/sbin/zpool status -gP",
                            "  sudo -n /usr/sbin/smartctl -a /dev/sda  ",
                            "sudo -n /usr/bin/sg_ses -p ec /dev/sg1",
                        ],
                    ),
                )
            ]
        )

        self.assertEqual(
            saved_sudo_commands_for_system(settings, "saved-scale"),
            [
                "sudo -n /usr/sbin/smartctl -a /dev/sda",
                "sudo -n /usr/bin/sg_ses -p ec /dev/sg1",
            ],
        )
        self.assertEqual(saved_sudo_commands_for_system(settings, None), [])
        self.assertEqual(saved_sudo_commands_for_system(settings, "   "), [])
        with self.assertRaisesRegex(ValueError, "saved SSH command list is unavailable"):
            saved_sudo_commands_for_system(settings, "removed-system")

    def test_saved_sudo_commands_feed_the_same_normalizer_as_the_request_list(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-scale",
                    ssh=SSHConfig(
                        enabled=True,
                        host="saved.example.test",
                        user="jbodmap",
                        commands=["sudo -n /usr/sbin/smartctl -a /dev/sda"],
                    ),
                )
            ]
        )
        saved = saved_sudo_commands_for_system(settings, "saved-scale")

        content = ServiceAccountBootstrapService._build_sudoers_content("jbodmap", "scale", saved)

        self.assertIn("/usr/sbin/smartctl -a /dev/sda", content)
        self.assertNotIn("/usr/bin/sg_ses -p aes /dev/sg*", content)
        self.assertNotIn("sudo -n", content)


def sudoers_grant_matches(grant: str, command: str) -> bool:
    """Model how sudoers matches a granted command spec against a real command.

    Wildcards are matched per argument, the way sudo compares an invoked command
    against a `Cmnd_Spec`, so `smartctl -x -j *` does not silently cover
    `smartctl -d scsi -x -j /dev/sda`.
    """

    grant_tokens = shlex.split(grant)
    command_tokens = shlex.split(command)
    if len(grant_tokens) != len(command_tokens):
        return False
    return all(
        fnmatch.fnmatchcase(command_token, grant_token)
        for grant_token, command_token in zip(grant_tokens, command_tokens)
    )


def strip_sudo_prefix(command: str) -> str | None:
    """Return the command sudo would match against sudoers, or None if not sudo."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "sudo":
        return None
    remainder = tokens[1:]
    while remainder and remainder[0].startswith("-"):
        remainder.pop(0)
    if not remainder:
        return None
    for index, token in enumerate(remainder):
        if token in {"&&", "||", ";"} or token.startswith((">", "2>")):
            remainder = remainder[:index]
            break
    return shlex.join(remainder)


class BootstrapSudoGrantContractTests(unittest.IsolatedAsyncioTestCase):
    """Bootstrap grants must cover the sudo-run builders for every SSH platform."""

    maxDiff = None
    platforms: tuple[Platform, ...] = ("core", "scale", "quantastor", "linux")

    def build_service(self, temp_dir: str, platform: Platform) -> InventoryService:
        system = SystemConfig(
            id=f"{platform}-contract",
            label=f"{platform.title()} contract",
            truenas=TrueNASConfig(platform=platform),
            ssh=SSHConfig(
                enabled=True,
                host=f"{platform}-host.invalid",
                user="jbodmap",
                commands=[],
            ),
        )
        return InventoryService(
            Settings(),
            system,
            AsyncMock(),
            AsyncMock(),
            None,
            MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
            ProfileRegistry(Settings()),
            SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
        )

    async def collect_sudo_commands(self, platform: Platform) -> set[str]:
        recorded: list[str] = []
        host = f"{platform}-host.invalid"

        async def record_many(commands, host=None, **_kwargs):
            command_list = list(commands)
            recorded.extend(command_list)
            return [
                SSHCommandResult(
                    command=command,
                    ok="smartctl" not in command,
                    stdout="",
                    stderr="command not found" if "smartctl" in command else "",
                    exit_code=127 if "smartctl" in command else 0,
                )
                for command in command_list
            ]

        async def record_one(command, host=None, **_kwargs):
            recorded.append(command)
            return SSHCommandResult(command=command, ok=True, stdout="", stderr="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.build_service(temp_dir, platform)
            service._run_ssh_commands = record_many
            service._run_ssh_command = record_one
            smartctl_binaries = service._smartctl_binary_candidates()

            # Commands seeded by the setup wizard for this platform.
            recorded.extend(default_ssh_commands_for_platform(platform))

            # Dynamic CORE collection probes are built from discovered adapters.
            if platform == "core":
                seed_commands = service._core_mprutil_seed_probe_commands([])
                recorded.extend(seed_commands)
                recorded.extend(
                    service._core_mprutil_unit_probe_commands(
                        [
                            SSHCommandResult(
                                command=seed_commands[0],
                                ok=True,
                                stdout="/dev/mpr10 Synthetic adapter",
                                stderr="",
                                exit_code=0,
                            )
                        ]
                    )
                )
                recorded.extend(service._core_mpr_dmesg_probe_commands([]))
                recorded.extend(service._core_pci_slot_probe_commands([]))

            # SES page reads for a discovered Linux-family SCSI enclosure.
            if platform in {"scale", "quantastor", "linux"}:
                await service._fetch_sg_ses_host_overlay(
                    host,
                    ["/dev/sg3"],
                    failure_prefix="SES",
                )

            # SMART reads use the same builder on every SSH-capable platform.
            if platform == "linux":
                await service._fetch_smart_summary_over_ssh(["nvme0n1"])
            await service._fetch_smart_summary_over_ssh(["sda"])
            await service._fetch_smart_summary_over_ssh(
                ["sda"],
                hosts=[host] if platform == "quantastor" else None,
                device_type=LINUX_BOOT_MEDIA_SMARTCTL_DEVICE_TYPE,
            )

            # Identify control uses sesutil on CORE and sg_ses elsewhere.
            ses_device = "/dev/ses3" if platform == "core" else "/dev/sg3"
            slot_view = SimpleNamespace(
                slot=3,
                slot_label="03",
                led_reason=None,
                ssh_ses_targets=[
                    {
                        "ses_device": ses_device,
                        "ses_element_id": 3,
                        "ses_slot_number": 3,
                        "ssh_host": host,
                    }
                ],
                ssh_ses_device=ses_device,
                ssh_ses_element_id=3,
            )
            await service._set_slot_led_over_ssh(slot_view, LedAction.identify)
            await service._set_slot_led_over_ssh(slot_view, LedAction.clear)

        sudo_commands = {
            stripped
            for command in recorded
            if (stripped := strip_sudo_prefix(command)) is not None
        }
        self.assertTrue(sudo_commands, f"no sudo-run {platform} commands were captured")
        for smartctl_binary in smartctl_binaries:
            self.assertIn(
                f"{smartctl_binary} -d scsi -x -j /dev/sda",
                sudo_commands,
                f"typed SMART JSON builder was not exercised for {platform}",
            )
            self.assertIn(
                f"{smartctl_binary} -d scsi -x /dev/sda",
                sudo_commands,
                f"typed SMART text builder was not exercised for {platform}",
            )
        return sudo_commands

    async def test_bootstrap_grants_cover_every_sudo_run_platform_command(self) -> None:
        for platform in self.platforms:
            with self.subTest(platform=platform):
                grants = SUDO_COMMANDS_BY_PLATFORM[platform]
                ungranted = sorted(
                    command
                    for command in await self.collect_sudo_commands(platform)
                    if not any(sudoers_grant_matches(grant, command) for grant in grants)
                )
                self.assertEqual(
                    ungranted,
                    [],
                    f"{platform} bootstrap sudo grants do not cover: " + ", ".join(ungranted),
                )

    async def test_supplemental_grants_cover_device_specific_platform_commands(self) -> None:
        # Commands built per device never appear in an operator's saved command
        # list, so they have to survive the supplemental merge too.
        for platform in self.platforms:
            with self.subTest(platform=platform):
                sudo_commands = await self.collect_sudo_commands(platform)
                requested_commands = [
                    f"sudo -n {command}"
                    for command in sudo_commands
                    if "smartctl -d " not in command
                ]
                grants = ServiceAccountBootstrapService._resolve_sudo_commands(
                    platform,
                    requested_commands,
                )
                ungranted = sorted(
                    command
                    for command in sudo_commands
                    if not any(sudoers_grant_matches(grant, command) for grant in grants)
                )
                self.assertEqual(
                    ungranted,
                    [],
                    f"{platform} supplemental sudo grants do not cover: " + ", ".join(ungranted),
                )

    def test_smartctl_grants_only_allow_inventory_read_shapes(self) -> None:
        allowed_argument_shapes = {
            ("-x", "-j", "*"),
            ("-x", "*"),
            ("-d", "*", "-x", "-j", "*"),
            ("-d", "*", "-x", "*"),
        }
        for platform in self.platforms:
            with self.subTest(platform=platform):
                for grant in SUDO_COMMANDS_BY_PLATFORM[platform]:
                    tokens = shlex.split(grant)
                    if Path(tokens[0]).name == "smartctl":
                        self.assertIn(tuple(tokens[1:]), allowed_argument_shapes)

    def test_quantastor_bootstrap_does_not_grant_unbounded_root_cli_access(self) -> None:
        self.assertFalse(
            any(
                grant.startswith("/usr/bin/qs")
                for grant in SUDO_COMMANDS_BY_PLATFORM["quantastor"]
            )
        )


if __name__ == "__main__":
    unittest.main()
