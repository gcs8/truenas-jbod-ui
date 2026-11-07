from __future__ import annotations

import contextlib
import fnmatch
import json
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from admin_service.services.account_bootstrap import (
    SMARTCTL_SUDO_COMMANDS,
    SUDO_COMMANDS_BY_PLATFORM,
    SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM,
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
from app.services.ssh_probe import SSHCommandResult, SSHProbe
from app.services.system_setup import default_ssh_commands_for_platform


Platform = Literal["core", "scale", "quantastor", "linux"]


class FakeProbe:
    last_config = None
    last_command = None
    last_stdin = None

    def __init__(self, config) -> None:
        type(self).last_config = config

    def run_command_sync(
        self,
        command: str,
        *,
        stdin_data: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SSHCommandResult:
        type(self).last_command = command
        type(self).last_stdin = stdin_data
        return SSHCommandResult(
            command=command,
            ok=True,
            stdout=(
                "BOOTSTRAP_SERVICE_USER=jbodmap\n"
                "BOOTSTRAP_SERVICE_HOME=/home/jbodmap\n"
                "BOOTSTRAP_AUTHORIZED_KEYS_PATH=/home/jbodmap/.ssh/authorized_keys\n"
                "BOOTSTRAP_SUDOERS_PATH=/etc/sudoers.d/truenas-jbod-ui-jbodmap\n"
                "BOOTSTRAP_PERMISSION_TARGET=/etc/sudoers.d/truenas-jbod-ui-jbodmap\n"
                "BOOTSTRAP_SUDO_RULES_INSTALLED=1\n"
            ),
            stderr="",
            exit_code=0,
        )


class ServiceAccountBootstrapServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeProbe.last_config = None
        FakeProbe.last_command = None
        FakeProbe.last_stdin = None

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
            self.assertEqual(FakeProbe.last_command, "/bin/sh -s")
            self.assertIn("midclt call user.update", str(FakeProbe.last_stdin))
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

    def test_bootstrap_uses_the_configured_known_hosts_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config" / "config.yaml"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            key_manager = SSHKeyManager(str(config_file))
            generated_key = key_manager.generate_keypair("id_truenas")
            configured = str(Path(temp_dir) / "host-trust" / "known_hosts")
            service = ServiceAccountBootstrapService(
                str(config_file),
                probe_factory=FakeProbe,
                known_hosts_path=configured,
            )

            result = service.bootstrap_service_account(
                SystemSetupBootstrapRequest(
                    platform="core",
                    host="nas.example.test",
                    bootstrap_user="root",
                    bootstrap_password="bootstrap-secret",
                    bootstrap_known_hosts_path=str(Path(temp_dir) / "request-selected-known-hosts"),
                    bootstrap_strict_host_key_checking=False,
                    service_user="jbodmap",
                    service_key_name=generated_key["name"],
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(FakeProbe.last_config.known_hosts_path, configured)

    def test_bootstrap_uses_sudo_and_private_key_path_for_non_root_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config" / "config.yaml"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            private_key_path = Path(temp_dir) / "manual_runtime_key"
            self.write_private_key(private_key_path)
            service = self.make_service(config_file)

            payload = SystemSetupBootstrapRequest(
                platform="scale",
                host="installer.example.test",
                bootstrap_user="installer",
                bootstrap_password="installer-secret",
                service_user="jbodmap",
                service_key_path=str(private_key_path),
            )

            result = service.bootstrap_service_account(payload)

            self.assertTrue(result["ok"])
            self.assertEqual(result["key_source"], str(private_key_path))
            self.assertEqual(FakeProbe.last_config.user, "installer")
            self.assertEqual(FakeProbe.last_config.sudo_password, "")
            self.assertEqual(FakeProbe.last_command, "sudo -n /bin/sh -s")

    def test_build_sudoers_content_formats_wrapped_commands_cleanly(self) -> None:
        content = ServiceAccountBootstrapService._build_sudoers_content("jbodmap", "core")

        self.assertIn("Cmnd_Alias JBODMAP_CORE_CMDS", content)
        self.assertIn("/usr/sbin/sesutil map", content)
        self.assertIn("/usr/sbin/mprutil show adapters", content)
        self.assertIn("/usr/sbin/mprutil ^-u [0-9]+ show expanders$", content)
        self.assertIn("/usr/sbin/sesutil ^locate -u /dev/ses[0-9]+ [0-9]+ on$", content)
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

        self.assertIn("/usr/sbin/mprutil ^-u [0-9]+ show expanders$", content)
        self.assertIn("/usr/sbin/mprutil ^-u [0-9]+ show iocfacts$", content)
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
        self.assertIn("/usr/sbin/mprutil ^-u [0-9]+ show expanders$", payload["sudo_commands"])
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
        self.assertIn("/usr/bin/sg_ses ^-p aes /dev/sg[0-9]+$", content)
        self.assertIn("/usr/bin/sg_ses ^-p ec /dev/sg[0-9]+$", content)
        self.assertIn("/usr/bin/sg_ses ^--join --filter /dev/sg[0-9]+$", content)
        self.assertIn(
            "/usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --set=ident /dev/sg[0-9]+$",
            content,
        )
        self.assertIn(
            "/usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --clear=ident /dev/sg[0-9]+$",
            content,
        )
        self.assertIn(
            "/usr/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-]",
            content,
        )
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


SYNTHETIC_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISyntheticBootstrapKey synthetic-bootstrap"
SYNTHETIC_CORE_HOME = "/mnt/tank/home/jbodmap"


class BootstrapRemoteExecTests(unittest.TestCase):
    """The exec string must survive any login shell, including csh for root on CORE (#947)."""

    platforms: tuple[Platform, ...] = ("core", "scale", "quantastor", "linux")

    def setUp(self) -> None:
        FakeProbe.last_config = None
        FakeProbe.last_command = None
        FakeProbe.last_stdin = None
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ServiceAccountBootstrapService(
            str(Path(self.temp.name) / "config.yaml"),
            probe_factory=FakeProbe,
        )

    def payload(self, platform: str, bootstrap_user: str = "root", **updates: object) -> SystemSetupBootstrapRequest:
        return SystemSetupBootstrapRequest(
            platform=platform,
            host="bootstrap.example.test",
            bootstrap_user=bootstrap_user,
            bootstrap_password="SYNTHETIC-BOOTSTRAP",
            service_user="jbodmap",
            service_public_key=SYNTHETIC_PUBLIC_KEY,
            **updates,
        )

    def test_exec_string_is_one_line_without_a_login_flag_on_every_platform(self) -> None:
        for platform in self.platforms:
            for bootstrap_user, expected in (("root", "/bin/sh -s"), ("installer", "sudo -n /bin/sh -s")):
                with self.subTest(platform=platform, bootstrap_user=bootstrap_user):
                    FakeProbe.last_command = None
                    FakeProbe.last_stdin = None
                    self.service.bootstrap_service_account(self.payload(platform, bootstrap_user))
                    command = str(FakeProbe.last_command)
                    self.assertEqual(command, expected)
                    self.assertNotIn("\n", command)
                    self.assertFalse(any(token.startswith("-l") for token in shlex.split(command)))
                    script = str(FakeProbe.last_stdin)
                    self.assertIn("set -eu", script)
                    self.assertIn(SYNTHETIC_PUBLIC_KEY, script)
                    self.assertTrue(script.endswith("\n"))

    def test_non_root_bootstrap_with_a_sudo_password_fails_before_connecting(self) -> None:
        payload = self.payload("scale", "installer", bootstrap_sudo_password="SYNTHETIC-BOOTSTRAP-SUDO")
        with self.assertRaises(ValueError) as raised:
            self.service.bootstrap_service_account(payload)
        message = str(raised.exception)
        self.assertIn("sudo password", message)
        self.assertIn("passwordless sudo", message)
        self.assertNotIn("SYNTHETIC-BOOTSTRAP-SUDO", message)
        self.assertIsNone(FakeProbe.last_config)
        self.assertIsNone(FakeProbe.last_command)

    def test_root_bootstrap_does_not_forward_an_unused_sudo_password(self) -> None:
        result = self.service.bootstrap_service_account(
            self.payload("scale", "root", bootstrap_sudo_password="SYNTHETIC-BOOTSTRAP-SUDO")
        )
        self.assertTrue(result["ok"])
        self.assertEqual(FakeProbe.last_command, "/bin/sh -s")
        self.assertEqual(FakeProbe.last_config.sudo_password, "")
        self.assertNotIn("SYNTHETIC-BOOTSTRAP-SUDO", str(FakeProbe.last_stdin))

    def test_probe_forwards_the_script_as_command_input(self) -> None:
        probe = SSHProbe(
            SSHConfig(enabled=True, host="bootstrap.example.test", user="root", strict_host_key_checking=False)
        )
        result = SSHCommandResult(command="/bin/sh -s", ok=True, stdout="", stderr="", exit_code=0)

        @contextlib.contextmanager
        def owned_client(_cancel):
            yield object(), False

        with (
            patch.object(probe, "_owned_client", side_effect=owned_client),
            patch.object(probe, "_run_single_command", return_value=result) as run_single,
        ):
            self.assertIs(probe.run_command_sync("/bin/sh -s", stdin_data="SYNTHETIC-SCRIPT\n"), result)
        self.assertEqual(run_single.call_args.args[1], "/bin/sh -s")
        self.assertEqual(run_single.call_args.kwargs["stdin_data"], "SYNTHETIC-SCRIPT\n")

    def test_core_script_never_creates_accounts_outside_the_middleware(self) -> None:
        for install_sudo_rules in (True, False):
            with self.subTest(install_sudo_rules=install_sudo_rules):
                script = self.service._build_remote_script(
                    self.payload("core", install_sudo_rules=install_sudo_rules),
                    SYNTHETIC_PUBLIC_KEY,
                )
                self.assertNotIn("useradd", script)
                self.assertNotRegex(script, r"(?m)^\s*pw\s")
                lookup = script.index("midclt call user.query")
                for change in ("mkdir ", "touch ", "chmod ", "chown ", ">>"):
                    self.assertLess(lookup, script.index(change), change)
        script = self.service._build_remote_script(self.payload("core"), SYNTHETIC_PUBLIC_KEY)
        self.assertIn('midclt call user.update "$svc_id"', script)
        linux = self.service._build_remote_script(self.payload("linux"), SYNTHETIC_PUBLIC_KEY)
        self.assertIn('useradd -m -s "$svc_shell" "$svc_user"', linux)

    def run_core_script(
        self,
        query_output: str,
        *,
        created_output: str = "[]",
        create_fails: bool = False,
        create_error: str = "[EINVAL] user_create.home: synthetic refusal",
        uid_output: str = "3001",
        uid_fails: bool = False,
        **updates: object,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        """Run the real CORE exec string and script against recording stubs.

        Every command that could change the host is a stub that only logs its
        call. When a csh is installed the exec string goes through it, as it
        does for root on TrueNAS CORE. `user.query` answers with
        ``query_output`` until `user.create` runs, then with ``created_output``.
        The `user.create` JSON argument is saved to ``self.created_payload``.
        Its synthetic CORE13 model reads uid for home permissions before the
        later automatic-allocation step, matching the installed ordering defect.
        This models that contract only, not complete middleware user creation.
        `grep` reports the key as present, as the middleware writes it when it
        creates the account.
        """
        run_temp = tempfile.TemporaryDirectory(dir=self.temp.name)
        self.addCleanup(run_temp.cleanup)
        root = Path(run_temp.name)
        stubs = root / "bin"
        stubs.mkdir()
        calls = root / "calls.log"
        midclt_input = root / "midclt.stdin"
        created_marker = root / "created"
        create_argument = root / "user-create.json"
        core13_create = root / "core13_create.py"
        core13_create.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "data = json.loads(sys.argv[1])\n"
            "with open(os.environ['CALL_LOG'], 'a') as log:\n"
            "    log.write('core13 group.create_internal\\n')\n"
            "    # CORE13 reads this before its later get_next_uid fallback.\n"
            "    uid = data['uid']\n"
            "    log.write(f'core13 filesystem.setperm uid={uid}\\n')\n"
            "if not data.get('uid'):\n"
            "    raise AssertionError('late UID allocation reached')\n"
            "Path(os.environ['CREATED_MARKER']).touch()\n"
            "print(61)\n"
        )
        (stubs / "midclt").write_text(
            "#!/bin/sh\n"
            'printf "midclt %s\\n" "$*" >> "$CALL_LOG"\n'
            'cat >> "$MIDCLT_INPUT"\n'
            'if [ "$2" = user.create ]; then\n'
            '  printf "%s" "$3" > "$CREATE_ARGUMENT"\n'
            '  if [ -n "$CREATE_FAILS" ]; then printf "%s\\n" "$CREATE_ERROR" >&2; exit 1; fi\n'
            f'  exec {shlex.quote(sys.executable)} "$CORE13_CREATE" "$3"\n'
            "fi\n"
            'if [ "$2" = user.get_next_uid ]; then\n'
            '  if [ -n "$UID_FAILS" ]; then echo "synthetic UID lookup failure" >&2; exit 1; fi\n'
            '  printf "%s\\n" "$UID_OUTPUT"\n'
            "fi\n"
            'if [ "$2" = user.query ]; then\n'
            '  if [ -e "$CREATED_MARKER" ]; then printf "%s\\n" "$CREATED_OUTPUT"; '
            'else printf "%s\\n" "$QUERY_OUTPUT"; fi\n'
            "fi\n"
        )
        for name in ("mkdir", "touch", "chmod", "chown", "pw", "useradd", "id", "grep"):
            (stubs / name).write_text('#!/bin/sh\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$CALL_LOG"\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        payload = self.payload("core", "root", **updates)
        self.service.bootstrap_service_account(payload)
        command = FakeProbe.last_command
        script = FakeProbe.last_stdin
        csh = shutil.which("csh") or shutil.which("tcsh")
        argv = [csh, "-f", "-c", command] if csh else ["/bin/sh", "-c", command]
        completed = subprocess.run(
            argv,
            input=script,
            capture_output=True,
            text=True,
            timeout=10,
            env={
                "PATH": f"{stubs}:/usr/bin:/bin",
                "HOME": str(root),
                "LC_ALL": "C",
                "CALL_LOG": str(calls),
                "MIDCLT_INPUT": str(midclt_input),
                "QUERY_OUTPUT": query_output,
                "CREATED_OUTPUT": created_output,
                "CREATED_MARKER": str(created_marker),
                "CREATE_ARGUMENT": str(create_argument),
                "CREATE_FAILS": "1" if create_fails else "",
                "CREATE_ERROR": create_error,
                "CORE13_CREATE": str(core13_create),
                "UID_OUTPUT": uid_output,
                "UID_FAILS": "1" if uid_fails else "",
            },
        )
        logged = calls.read_text().splitlines() if calls.exists() else []
        received = midclt_input.read_text() if midclt_input.exists() else ""
        self.created_payload = (
            json.loads(create_argument.read_text()) if create_argument.exists() else None
        )
        return completed, logged, received

    def assert_only_the_account_lookup_ran(self, completed, logged, received) -> None:
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("Nothing was changed on the host.", completed.stderr)
        self.assertNotIn("BOOTSTRAP_", completed.stdout)
        self.assertEqual(len(logged), 1, logged)
        self.assertTrue(logged[0].startswith("midclt call user.query "), logged)
        self.assertIn('"select"', logged[0])
        # Commands the script runs must not read the rest of the script from stdin.
        self.assertEqual(received, "")

    def test_core_missing_service_user_fails_closed_before_any_change(self) -> None:
        completed, logged, received = self.run_core_script("[]")
        self.assert_only_the_account_lookup_ran(completed, logged, received)
        for text in ("jbodmap", "Accounts > Users", "/mnt", "shell sh", "Disable Password", "public key"):
            self.assertIn(text, completed.stderr)

    def test_core_service_user_without_a_mnt_home_fails_closed_before_any_change(self) -> None:
        completed, logged, received = self.run_core_script(
            '[{"id": 54, "username": "jbodmap", "home": "/nonexistent"}]'
        )
        self.assert_only_the_account_lookup_ran(completed, logged, received)
        self.assertIn("/nonexistent", completed.stderr)
        self.assertIn("/mnt", completed.stderr)

    # --- #947 follow-up: create a missing CORE account through the middleware ---

    def expected_user_create(self, home: str = SYNTHETIC_CORE_HOME) -> dict[str, object]:
        return {
            "username": "jbodmap",
            "full_name": "TrueNAS JBOD UI service account",
            "group_create": True,
            "home": home,
            "home_mode": "700",
            "shell": "/bin/sh",
            "password_disabled": True,
            "smb": False,
            "sshpubkey": SYNTHETIC_PUBLIC_KEY,
        }

    def test_core_user_create_payload_is_exact_and_carries_no_password(self) -> None:
        payload = json.loads(
            ServiceAccountBootstrapService._build_core_midclt_user_create_payload(
                "jbodmap", "/bin/sh", SYNTHETIC_CORE_HOME, SYNTHETIC_PUBLIC_KEY
            )
        )
        self.assertEqual(payload, self.expected_user_create())
        self.assertNotIn("password", payload)
        self.assertIs(payload["smb"], False)
        self.assertIs(payload["password_disabled"], True)

    def test_core_missing_user_with_home_outside_a_pool_folder_fails_before_any_change(self) -> None:
        for bad_home in (
            "/home/x",
            "/mnt",
            "/mnt/",
            "/mnt/tank",
            "/mnt/tank/",
            "mnt/tank/home/jbodmap",
            "/mnt/../etc",
            "/mnt/tank/../../etc",
            "/mnt/tank/./jbodmap",
            "/mnt//tank/jbodmap",
            "/mnt/tank:x/jbodmap",
            "/mnt/tank/home\njbodmap",
            "/mnt/tank/home\x00jbodmap",
            "/mnt/tank/home jbodmap",
            "/mnt/tank/$(id)",
            "/mnt/tank/home/'jbodmap'",
        ):
            with self.subTest(bad_home=bad_home):
                completed, logged, received = self.run_core_script("[]", service_home=bad_home)
                self.assert_only_the_account_lookup_ran(completed, logged, received)
                self.assertIsNone(self.created_payload)
                self.assertIn("Home directory", completed.stderr)
                self.assertIn("/mnt/tank/home/jbodmap", completed.stderr)
                self.assertNotIn("validation error", completed.stderr)

    def test_core_service_home_accepts_folders_on_a_pool(self) -> None:
        for home, expected in (
            (SYNTHETIC_CORE_HOME, SYNTHETIC_CORE_HOME),
            ("/mnt/tank/jbodmap", "/mnt/tank/jbodmap"),
            ("  /mnt/tank/home/jbodmap/  ", SYNTHETIC_CORE_HOME),
            ("/mnt/tank/home/svc_jbod-map.1", "/mnt/tank/home/svc_jbod-map.1"),
        ):
            with self.subTest(home=home):
                self.service.bootstrap_service_account(self.payload("core", service_home=home))
                self.assertIn(
                    shlex.quote(
                        ServiceAccountBootstrapService._build_core_midclt_user_create_payload(
                            "jbodmap", "/bin/sh", expected, SYNTHETIC_PUBLIC_KEY
                        )
                    ),
                    str(FakeProbe.last_stdin),
                )

    def test_service_home_is_ignored_off_core(self) -> None:
        for platform in ("scale", "quantastor", "linux"):
            with self.subTest(platform=platform):
                self.service.bootstrap_service_account(self.payload(platform, service_home="/home/x"))
                self.assertNotIn("user.create", str(FakeProbe.last_stdin))

    def test_core_missing_service_user_with_a_home_is_created_through_the_middleware(self) -> None:
        completed, logged, received = self.run_core_script(
            "[]",
            created_output=f'[{{"id": 61, "username": "jbodmap", "home": "{SYNTHETIC_CORE_HOME}"}}]',
            service_home=SYNTHETIC_CORE_HOME,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.created_payload, {**self.expected_user_create(), "uid": 3001})
        midclt_calls = [line.split(" ", 3)[2] for line in logged if line.startswith("midclt ")]
        self.assertEqual(midclt_calls, ["user.query", "user.get_next_uid", "user.create", "user.query", "user.update"])
        self.assertIn("core13 filesystem.setperm uid=3001", logged)
        self.assertTrue(any(line.startswith("midclt call user.update 61 ") for line in logged), logged)
        create_index = next(i for i, line in enumerate(logged) if line.startswith("midclt call user.create "))
        for change in ("mkdir ", "chmod ", "chown ", "touch "):
            self.assertLess(create_index, next(i for i, line in enumerate(logged) if line.startswith(change)), change)
        self.assertFalse(any(line.startswith(("pw ", "useradd ")) for line in logged), logged)
        self.assertIn(f"BOOTSTRAP_SERVICE_HOME={SYNTHETIC_CORE_HOME}\n", completed.stdout)
        self.assertIn("BOOTSTRAP_ACCOUNT_CREATED=1\n", completed.stdout)
        self.assertIn("BOOTSTRAP_PERMISSION_TARGET=midclt:user.update:61\n", completed.stdout)
        self.assertEqual(received, "")

    def test_core_uid_allocation_precedes_create_and_home_permissions(self) -> None:
        for uid in (1000, 4017, 2147483647):
            for install_sudo_rules in (True, False):
                with self.subTest(uid=uid, install_sudo_rules=install_sudo_rules):
                    completed, logged, received = self.run_core_script(
                        "[]", uid_output=str(uid), service_home=SYNTHETIC_CORE_HOME,
                        created_output=f'[{json.dumps({"id": 61, "home": SYNTHETIC_CORE_HOME})}]',
                        install_sudo_rules=install_sudo_rules,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(self.created_payload, {**self.expected_user_create(), "uid": uid})
                    self.assertEqual(logged[1], "midclt call user.get_next_uid")
                    self.assertTrue(logged[2].startswith("midclt call user.create "), logged)
                    self.assertEqual(logged[3:5], [
                        "core13 group.create_internal", f"core13 filesystem.setperm uid={uid}",
                    ])
                    self.assertEqual(received, "")

    def test_core_malformed_uid_fails_before_create_or_host_changes(self) -> None:
        for malformed in (
            "", "null", "true", "false", "[]", "{}", '"3001"', '{"uid":3001}',
            "0", "1", "999", "65534", "-1", "2147483648", "4294967295",
            "999999999999999999999999999999", "03001", "+3001", "3001.0", "3e3",
            " 3001", "3001 ", "\n".join(("3001", "3002")), "３００１",
            '3001,"sudo":true', "$(id)", "3001; id", "3001`id`",
        ):
            with self.subTest(malformed=malformed):
                completed, logged, received = self.run_core_script(
                    "[]", uid_output=malformed, service_home=SYNTHETIC_CORE_HOME,
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertIn("UID", completed.stderr)
                self.assertIn("Nothing was changed on the host.", completed.stderr)
                self.assertEqual([line.split(" ", 3)[2] for line in logged], ["user.query", "user.get_next_uid"])
                self.assertIsNone(self.created_payload)
                self.assertNotIn("BOOTSTRAP_", completed.stdout)
                self.assertEqual(received, "")

    def test_core_failed_uid_lookup_fails_before_create_or_host_changes(self) -> None:
        completed, logged, received = self.run_core_script(
            "[]", uid_fails=True, service_home=SYNTHETIC_CORE_HOME,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("UID", completed.stderr)
        self.assertIn("Nothing was changed on the host.", completed.stderr)
        self.assertEqual([line.split(" ", 3)[2] for line in logged], ["user.query", "user.get_next_uid"])
        self.assertIsNone(self.created_payload)
        self.assertEqual(received, "")

    def test_core_missing_service_user_without_a_home_points_at_the_home_field(self) -> None:
        completed, logged, received = self.run_core_script("[]")
        self.assert_only_the_account_lookup_ran(completed, logged, received)
        self.assertIsNone(self.created_payload)
        self.assertIn("Home directory", completed.stderr)
        self.assertIn("Accounts > Users", completed.stderr)

    def test_core_uid_collision_is_reported_without_reallocation_or_retry(self) -> None:
        completed, logged, received = self.run_core_script(
            "[]", create_fails=True, create_error="[EEXIST] user_create.uid: synthetic collision",
            service_home=SYNTHETIC_CORE_HOME,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("synthetic collision", completed.stderr)
        self.assertEqual([line.split(" ", 3)[2] for line in logged], ["user.query", "user.get_next_uid", "user.create"])
        self.assertEqual(self.created_payload, {**self.expected_user_create(), "uid": 3001})
        self.assertNotIn("BOOTSTRAP_", completed.stdout)
        self.assertEqual(received, "")

    def test_core_existing_service_user_ignores_the_home_field(self) -> None:
        completed, logged, received = self.run_core_script(
            '[{"id": 54, "username": "jbodmap", "home": "/mnt/tank/svc/jbodmap"}]',
            service_home=SYNTHETIC_CORE_HOME,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(self.created_payload)
        self.assertFalse(any("user.create" in line for line in logged), logged)
        self.assertFalse(any("user.get_next_uid" in line for line in logged), logged)
        self.assertTrue(any(line.startswith("midclt call user.update 54 ") for line in logged), logged)
        self.assertIn("BOOTSTRAP_SERVICE_HOME=/mnt/tank/svc/jbodmap\n", completed.stdout)
        self.assertIn("BOOTSTRAP_ACCOUNT_CREATED=0\n", completed.stdout)
        self.assertEqual(received, "")

    def test_core_existing_service_user_ignores_an_invalid_home_field(self) -> None:
        for install_sudo_rules in (True, False):
            for home in ("/home/jbodmap", "/mnt/tank/../jbodmap", "/mnt/tank/$(id)"):
                with self.subTest(home=home, install_sudo_rules=install_sudo_rules):
                    completed, logged, received = self.run_core_script(
                        '[{"id": 54, "username": "jbodmap", "home": "/mnt/tank/svc/jbodmap"}]',
                        service_home=home,
                        install_sudo_rules=install_sudo_rules,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertIsNone(self.created_payload)
                    self.assertNotIn(home, "\n".join(logged))
                    self.assertNotIn("create_payload=", str(FakeProbe.last_stdin))
                    self.assertFalse(any("user.create" in line for line in logged), logged)
                    self.assertIn("BOOTSTRAP_SERVICE_HOME=/mnt/tank/svc/jbodmap\n", completed.stdout)
                    self.assertIn("BOOTSTRAP_ACCOUNT_CREATED=0\n", completed.stdout)
                    for change in ("mkdir ", "touch ", "chmod ", "chown "):
                        self.assertTrue(any(line.startswith(change) for line in logged), logged)
                    updates = [line for line in logged if line.startswith("midclt call user.update ")]
                    self.assertEqual(len(updates), int(install_sudo_rules))
                    if updates:
                        self.assertTrue(updates[0].startswith("midclt call user.update 54 "), updates)
                    self.assertEqual(received, "")

    def test_core_existing_service_user_outside_mnt_is_never_moved(self) -> None:
        completed, logged, received = self.run_core_script(
            '[{"id": 54, "username": "jbodmap", "home": "/nonexistent"}]',
            service_home=SYNTHETIC_CORE_HOME,
        )
        self.assert_only_the_account_lookup_ran(completed, logged, received)
        self.assertIsNone(self.created_payload)

    def test_core_invalid_home_field_does_not_bypass_existing_account_home_guard(self) -> None:
        completed, logged, received = self.run_core_script(
            '[{"id": 54, "username": "jbodmap", "home": "/nonexistent"}]',
            service_home="/home/jbodmap",
        )
        self.assert_only_the_account_lookup_ran(completed, logged, received)
        self.assertIsNone(self.created_payload)
        self.assertIn("/nonexistent", completed.stderr)

    def test_core_refused_user_create_stops_before_the_key_and_grants(self) -> None:
        completed, logged, received = self.run_core_script(
            "[]", create_fails=True, service_home=SYNTHETIC_CORE_HOME
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("synthetic refusal", completed.stderr)
        self.assertIn("could not create", completed.stderr)
        self.assertIn(SYNTHETIC_CORE_HOME, completed.stderr)
        self.assertNotIn("BOOTSTRAP_", completed.stdout)
        self.assertEqual(
            [line.split(" ", 3)[2] for line in logged],
            ["user.query", "user.get_next_uid", "user.create"],
        )
        self.assertEqual(received, "")

    def test_core_created_account_that_cannot_be_found_stops_before_the_key(self) -> None:
        completed, logged, received = self.run_core_script(
            "[]", created_output="[]", service_home=SYNTHETIC_CORE_HOME
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("jbodmap", completed.stderr)
        self.assertNotIn("BOOTSTRAP_", completed.stdout)
        self.assertEqual(
            [line.split(" ", 3)[2] for line in logged if line.startswith("midclt ")],
            ["user.query", "user.get_next_uid", "user.create", "user.query"],
        )

    def test_bootstrap_result_reports_a_created_account(self) -> None:
        stdout = (
            "BOOTSTRAP_SERVICE_USER=jbodmap\n"
            f"BOOTSTRAP_SERVICE_HOME={SYNTHETIC_CORE_HOME}\n"
            "BOOTSTRAP_ACCOUNT_CREATED=1\n"
            "BOOTSTRAP_SUDO_RULES_INSTALLED=1\n"
        )
        with patch.object(FakeProbe, "run_command_sync", return_value=SSHCommandResult(
            command="/bin/sh -s", ok=True, stdout=stdout, stderr="", exit_code=0,
        )):
            result = self.service.bootstrap_service_account(
                self.payload("core", service_home=SYNTHETIC_CORE_HOME)
            )
        self.assertTrue(result["account_created"])
        self.assertIn("Created jbodmap", str(result["detail"]))
        with patch.object(FakeProbe, "run_command_sync", return_value=SSHCommandResult(
            command="/bin/sh -s", ok=True, stdout=stdout.replace("CREATED=1", "CREATED=0"), stderr="", exit_code=0,
        )):
            result = self.service.bootstrap_service_account(self.payload("core"))
        self.assertFalse(result["account_created"])


class BootstrapPolicyEvidenceTests(unittest.TestCase):
    def payload(self):
        return SystemSetupBootstrapRequest(
            platform="linux", host="bootstrap.example.test", bootstrap_user="root",
            bootstrap_password="synthetic-password", service_user="jbodmap",
            service_public_key="ssh-ed25519 synthetic",
        )

    def run_policy(self, policy, *, both=False, validator="ok", existing=True, active="etc"):
        """Execute only the real policy block, with all paths and visudo remapped.

        No account/key operations, real sudo, or host policy files are executed.
        The validator is a shell function, never the host visudo executable.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            etc = root / "etc/sudoers.d"
            local = root / "usr/local/etc/sudoers.d"
            etc.parent.mkdir(parents=True)
            if existing:
                etc.mkdir()
            if both:
                local.mkdir(parents=True)
            target_dir = etc if active == "etc" else local
            source = root / "etc/sudoers"
            def remap(text):
                text = text.replace('/usr/local/etc/', '__LOCAL_ETC__/').replace('/etc/', '__ETC__/')
                return text.replace('__LOCAL_ETC__', str(root / 'usr/local/etc')).replace(
                    '__ETC__', str(root / 'etc'))

            if policy is not None:
                source.write_text(remap(policy))
            service = ServiceAccountBootstrapService(str(root / "config.yaml"), probe_factory=FakeProbe)
            script = service._build_remote_script(self.payload(), "ssh-ed25519 synthetic")
            start = script.index("  sudoers_dir=")
            end = script.index('  permission_target="$sudoers_path"', start)
            block = script[start:end + len('  permission_target="$sudoers_path"')]
            block = remap(block)
            self.assertNotIn('useradd', block)
            self.assertNotIn('authorized_keys', block)
            # timeout invokes our shell function directly; it never launches real visudo.
            stub = (
                'visudo() {\n'
                + ('return 1\n' if validator == 'fail' else
                   'if [ "$1" = "-c" ]; then\n'
                   f"printf '%s: parsed OK\\n' {shlex.quote(str(source))}\n"
                   + (f"for f in {shlex.quote(str(target_dir))}/truenas-jbod-ui-*; do\n"
                      'case "$f" in *.tmp*) continue;; esac\n'
                      '[ ! -f "$f" ] || printf "%s: parsed OK\\n" "$f"\ndone\n'
                      if validator == 'ok' else '') + 'fi\nreturn 0\n')
                + '}\ntimeout() { shift; "$@"; }\n'
            )
            completed = subprocess.run(
                ['/bin/sh', '-c', 'set -eu\n' + stub + block + '\nprintf "%s\\n" "$permission_target"'],
                capture_output=True, text=True, timeout=5,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
            )
            return completed, str(target_dir / 'truenas-jbod-ui-jbodmap'), local.exists()

    def test_active_etc_include_wins_with_both_directories_existing(self):
        result, target, _ = self.run_policy('#includedir /etc/sudoers.d\n', both=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), target)

    def test_active_include_does_not_create_an_unincluded_local_directory(self):
        result, target, local_exists = self.run_policy('@includedir /etc/sudoers.d\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), target)
        self.assertFalse(local_exists)

    def test_explicit_local_include_and_commented_decoy_use_the_active_directory(self):
        result, target, _ = self.run_policy(
            '# @includedir /etc/sudoers.d\n  @includedir /usr/local/etc/sudoers.d # active\n',
            both=True, active="local",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), target)

    def test_active_missing_directory_can_be_created(self):
        result, target, _ = self.run_policy('#includedir /etc/sudoers.d\n', existing=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), target)

    def test_nonincluded_and_unsupported_policy_layouts_fail_closed(self):
        policies = [None, '', '# comment /etc/sudoers.d\n', '# #includedir /etc/sudoers.d\n',
                    '# includedir /etc/sudoers.d\n', '@include /etc/nested\n',
                    '#include /etc/nested\n', '@includedir "/etc/sudoers.d"\n',
                    '@includedir /etc/sudoers.d.extra\n',
                    '@includedir /etc/sudoers.d\n@include /etc/nested\n',
                    'Defaults env_keep += "\\\n#includedir /etc/sudoers.d\n"\n',
                    '#' * 65537 + '\n#includedir /etc/sudoers.d\n']
        for policy in policies:
            with self.subTest(policy=repr(policy)[:100]):
                result, _, _ = self.run_policy(policy, both=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('sudo', result.stderr.lower())
                self.assertEqual(result.stdout, '')

    def test_failed_or_nonincluding_validator_never_reports_policy_success(self):
        for validator in ('fail', 'omits_target'):
            with self.subTest(validator=validator):
                result, _, _ = self.run_policy('#includedir /etc/sudoers.d\n', validator=validator)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_mocked_ssh_success_without_policy_receipt_is_not_grant_success(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ServiceAccountBootstrapService(str(Path(temp) / 'config.yaml'), probe_factory=FakeProbe)
            with patch.object(FakeProbe, 'run_command_sync', return_value=SSHCommandResult(
                command='synthetic', ok=True, stdout='BOOTSTRAP_SUDOERS_PATH=/etc/sudoers.d/synthetic\n',
                stderr='', exit_code=0,
            )):
                result = service.bootstrap_service_account(self.payload())
            self.assertFalse(result['ok'])
            self.assertFalse(result['sudo_rules_installed'])
            self.assertIn('could not be verified', result['detail'])

    def test_platform_boundaries_and_disabled_policy_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ServiceAccountBootstrapService(str(Path(temp) / "config.yaml"), probe_factory=FakeProbe)
            payload = self.payload()
            core = payload.model_copy(update={"platform": "core"})
            script = service._build_remote_script(core, "ssh-ed25519 synthetic")
            self.assertIn("midclt call user.update", script)
            self.assertNotIn("check_sudo_policy", script)
            with self.assertRaisesRegex(ValueError, "ESXi does not use"):
                service.bootstrap_service_account(payload.model_copy(update={"platform": "esxi"}))
            result = service.bootstrap_service_account(payload.model_copy(update={"install_sudo_rules": False}))
            self.assertTrue(result["ok"])
            self.assertFalse(result["sudo_rules_installed"])

    def test_mocked_ssh_policy_failure_is_not_grant_success(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ServiceAccountBootstrapService(str(Path(temp) / 'config.yaml'), probe_factory=FakeProbe)
            with patch.object(FakeProbe, 'run_command_sync', return_value=SSHCommandResult(
                command='synthetic', ok=False, stdout='', stderr='Unable to verify sudo policy.', exit_code=1
            )), self.assertRaisesRegex(ValueError, 'verify sudo policy'):
                service.bootstrap_service_account(self.payload())


def sudoers_grant_matches(grant: str, command: str) -> bool:
    """Model how sudoers matches a granted command spec against a real command.

    Sudo joins argv with spaces before matching the argument portion of a
    ``Cmnd_Spec``. Globs can therefore cross argument boundaries; anchored
    regular expressions cannot.
    """

    grant_tokens = shlex.split(grant)
    command_tokens = shlex.split(command)
    if not grant_tokens or not command_tokens or grant_tokens[0] != command_tokens[0]:
        return False
    grant_arguments = grant.split(maxsplit=1)[1] if len(grant_tokens) > 1 else ""
    command_arguments = " ".join(command_tokens[1:])
    if grant_arguments.startswith("^") and grant_arguments.endswith("$"):
        return re.fullmatch(grant_arguments, command_arguments) is not None
    return fnmatch.fnmatchcase(command_arguments, grant_arguments)


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


CORE_ABSENT_SMARTCTL_PREFIX = "/usr/sbin/smartctl "
CORE_SMART_ALLOWED = (
    "/usr/local/sbin/smartctl -x -j /dev/da0",
    "/usr/local/sbin/smartctl -x /dev/da92",
    "/usr/local/sbin/smartctl -x -j /dev/multipath/disk7",
    "/usr/local/sbin/smartctl -x /dev/multipath/disk7",
    "/usr/local/sbin/smartctl -x -j /dev/nvd0",
    "/usr/local/sbin/smartctl -x -j /dev/ada1",
)
CORE_SMART_REFUSED = (
    "/usr/local/sbin/smartctl -x -t long /dev/da0",
    "/usr/local/sbin/smartctl -x -s wcache,off /dev/da0",
    "/usr/local/sbin/smartctl -x -j /dev/da0 -t short",
    "/usr/local/sbin/smartctl -x -j /dev/../etc/master.passwd",
    "/usr/local/sbin/smartctl -x -j /dev/a/b/c/d",
)


def middleware_stored_grant(grant: str) -> str:
    """Mirror TrueNAS CORE 13.0 ``sudoers.mako``: backslash-escape \\ , : and =."""

    for character in ("\\", ",", ":", "="):
        grant = grant.replace(character, "\\" + character)
    return grant


def core_stored_grant_matches(grant: str, command: str) -> bool:
    """Match a CORE grant the way sudo sees it once the middleware has stored it.

    Python ``re`` stands in for sudo's POSIX ERE. The middleware's backslashes
    reach the regex unchanged, so ``{0\\,2}`` is no longer an interval (#943).
    The sudoers parser removes them from literal arguments.
    """

    stored = middleware_stored_grant(grant)
    path, _, arguments = stored.partition(" ")
    command_tokens = shlex.split(command)
    if not command_tokens or path != command_tokens[0]:
        return False
    command_arguments = " ".join(command_tokens[1:])
    if arguments.startswith("^") and arguments.endswith("$"):
        pattern = arguments.replace("[[]", r"\[").replace("[]]", r"\]")
        try:
            return re.fullmatch(pattern, command_arguments) is not None
        except re.error:
            return False
    return fnmatch.fnmatchcase(command_arguments, re.sub(r"\\(.)", r"\1", arguments))


def grant_covers(platform: str, grants, command: str) -> bool:
    matcher = core_stored_grant_matches if platform == "core" else sudoers_grant_matches
    return any(matcher(grant, command) for grant in grants)


def core_grant_sources() -> dict[str, tuple[str, ...]]:
    payload = json.loads(ServiceAccountBootstrapService._build_core_midclt_user_update_payload())
    return {
        "seeded": SUDO_COMMANDS_BY_PLATFORM["core"],
        "supplemental": SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM["core"],
        "user.update payload": tuple(payload["sudo_commands"]),
    }


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
        smartctl_success = False

        async def record_many(commands, host=None, **_kwargs):
            command_list = list(commands)
            recorded.extend(command_list)
            return [
                SSHCommandResult(
                    command=command,
                    ok="smartctl" not in command or smartctl_success,
                    stdout=(
                        '{"device":{"protocol":"SCSI"},"smart_status":{"passed":true}}'
                        if smartctl_success and "smartctl" in command and " -j " in command
                        else ""
                    ),
                    stderr=(
                        "command not found"
                        if "smartctl" in command and not smartctl_success
                        else ""
                    ),
                    exit_code=127 if "smartctl" in command and not smartctl_success else 0,
                )
                for command in command_list
            ]

        async def record_one(command, host=None, **_kwargs):
            recorded.append(command)
            # The locate shelf check reads the enclosure device first; it still
            # reports the synthetic shelf, so the locate commands run too.
            stdout = ""
            if command.endswith("/usr/sbin/sesutil map"):
                stdout = "ses3:\n  Enclosure ID: synthetic-shelf\n"
            elif "/usr/bin/sg_ses -p aes " in command:
                stdout = "  Primary enclosure logical identifier (hex): synthetic-shelf\n"
            return SSHCommandResult(command=command, ok=True, stdout=stdout, stderr="", exit_code=0)

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
            smartctl_success = True
            for smartctl_binary in smartctl_binaries:
                with patch.object(
                    service,
                    "_smartctl_binary_candidates",
                    return_value=(smartctl_binary,),
                ):
                    await service._fetch_smart_summary_over_ssh(
                        ["sda"],
                        hosts=[host] if platform == "quantastor" else None,
                        device_type=None if platform == "core" else LINUX_BOOT_MEDIA_SMARTCTL_DEVICE_TYPE,
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
                        "enclosure_id": "synthetic-shelf",
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
        # CORE disks never carry a SMART device type; only Linux boot media do (#943).
        type_arguments = "" if platform == "core" else f"-d {LINUX_BOOT_MEDIA_SMARTCTL_DEVICE_TYPE} "
        for smartctl_binary in smartctl_binaries:
            self.assertIn(
                f"{smartctl_binary} {type_arguments}-x -j /dev/sda",
                sudo_commands,
                f"typed SMART JSON builder was not exercised for {platform}",
            )
            self.assertIn(
                f"{smartctl_binary} {type_arguments}-x /dev/sda",
                sudo_commands,
                f"typed SMART text builder was not exercised for {platform}",
            )
        if platform == "core":
            # CORE installs smartctl only under /usr/local/sbin. The inventory's
            # /usr/sbin fallback cannot run there, and the middleware refuses a
            # grant that names a missing executable.
            sudo_commands = {
                command for command in sudo_commands if not command.startswith(CORE_ABSENT_SMARTCTL_PREFIX)
            }
        return sudo_commands

    async def test_bootstrap_grants_cover_every_sudo_run_platform_command(self) -> None:
        for platform in self.platforms:
            with self.subTest(platform=platform):
                grants = SUDO_COMMANDS_BY_PLATFORM[platform]
                ungranted = sorted(
                    command
                    for command in await self.collect_sudo_commands(platform)
                    if not grant_covers(platform, grants, command)
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
                    if not grant_covers(platform, grants, command)
                )
                self.assertEqual(
                    ungranted,
                    [],
                    f"{platform} supplemental sudo grants do not cover: " + ", ".join(ungranted),
                )

    def test_no_grant_uses_a_glob_that_can_cross_arguments(self) -> None:
        # Sudo joins argv with spaces before matching a glob, so `*` can match
        # extra arguments. Every argument pattern must be an anchored regex or
        # an exact command.
        from admin_service.services.account_bootstrap import SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM

        for source in (SUDO_COMMANDS_BY_PLATFORM, SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM):
            for platform, grants in source.items():
                for grant in grants:
                    with self.subTest(platform=platform, grant=grant):
                        arguments = grant.split(maxsplit=1)[1] if " " in grant else ""
                        if arguments.startswith("^") and arguments.endswith("$"):
                            continue
                        self.assertFalse(
                            any(character in arguments for character in "*?["),
                            "argument glob can match extra arguments",
                        )

    def test_mprutil_sesutil_and_nvme_grants_refuse_extra_arguments(self) -> None:
        cases = {
            "core": (
                (
                    "/usr/sbin/mprutil -u 0 show adapter",
                    "/usr/sbin/mprutil -u 10 show expanders",
                    "/usr/sbin/sesutil locate -u /dev/ses5 3 on",
                    "/usr/sbin/sesutil locate -u /dev/ses12 59 off",
                ),
                (
                    # The unit glob used to admit a whole mprutil command line.
                    "/usr/sbin/mprutil -u 0 flash save /tmp/synthetic show adapter",
                    "/usr/sbin/mprutil -u 0 show adapter extra",
                    "/usr/sbin/sesutil locate -u /dev/ses5 all on",
                    "/usr/sbin/sesutil locate -u /dev/ses5 3 on -u /dev/ses6 4 on",
                    "/usr/sbin/sesutil locate -u /dev/ses5 3 fault on",
                ),
            ),
            "linux": (
                (
                    "/usr/sbin/nvme smart-log -o json /dev/nvme0",
                    "/usr/sbin/nvme id-ctrl -o json /dev/nvme12",
                    "/usr/sbin/nvme id-ns -o json /dev/nvme0n1",
                ),
                (
                    "/usr/sbin/nvme smart-log -o json /dev/nvme0 --namespace-id=2",
                    "/usr/sbin/nvme id-ns -o json /dev/nvme0n1 extra",
                    "/usr/sbin/nvme id-ctrl -o json /dev/nvme0 -o binary",
                ),
            ),
        }
        from admin_service.services.account_bootstrap import SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM

        for platform, (allowed, refused) in cases.items():
            for source in (SUDO_COMMANDS_BY_PLATFORM, SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM):
                grants = source[platform]
                for command in allowed:
                    with self.subTest(platform=platform, allowed=command):
                        self.assertTrue(any(sudoers_grant_matches(grant, command) for grant in grants))
                for command in refused:
                    with self.subTest(platform=platform, refused=command):
                        self.assertFalse(any(sudoers_grant_matches(grant, command) for grant in grants))

    def test_sg_ses_grants_reject_options_after_the_device_argument(self) -> None:
        injected = (
            "/usr/bin/sg_ses -p aes /dev/sg3 "
            "--control --data=synthetic-nonsecret-payload"
        )
        legitimate = (
            "/usr/bin/sg_ses -p aes /dev/sg3",
            "/usr/bin/sg_ses -p ec /dev/sg4095",
            "/usr/bin/sg_ses --join --filter /dev/sg12",
            "/usr/bin/sg_ses --dev-slot-num=7 --set=ident /dev/sg12",
            "/usr/bin/sg_ses --dev-slot-num=7 --clear=ident /dev/sg12",
        )
        for platform in ("scale", "linux", "quantastor"):
            with self.subTest(platform=platform):
                grants = SUDO_COMMANDS_BY_PLATFORM[platform]
                self.assertFalse(
                    any(sudoers_grant_matches(grant, injected) for grant in grants),
                    f"{platform} grants admitted trailing sg_ses control options",
                )
                for command in legitimate:
                    self.assertTrue(
                        any(sudoers_grant_matches(grant, command) for grant in grants),
                        f"{platform} grants did not cover {command}",
                    )

    def test_smartctl_grants_only_allow_inventory_read_shapes(self) -> None:
        for platform in self.platforms:
            with self.subTest(platform=platform):
                for grant in SUDO_COMMANDS_BY_PLATFORM[platform]:
                    tokens = shlex.split(grant)
                    if Path(tokens[0]).name == "smartctl":
                        arguments = grant.split(maxsplit=1)[1]
                        self.assertTrue(arguments.startswith("^"), grant)
                        self.assertTrue(arguments.endswith("$"), grant)

    def test_smartctl_grants_reject_mutating_and_trailing_options(self) -> None:
        legitimate = (
            "/usr/sbin/smartctl -x -j /dev/sda",
            "/usr/sbin/smartctl -x /dev/disk/by-id/scsi-synthetic.01",
            "/usr/sbin/smartctl -d scsi -x -j /dev/boot",
            "/usr/local/sbin/smartctl -d megaraid,12 -x /dev/da0",
        )
        rejected = (
            "/usr/sbin/smartctl -x -j /dev/sda --smart=off",
            "/usr/sbin/smartctl -x /dev/sda --set=wcache,off",
            "/usr/sbin/smartctl -d scsi -x -j /dev/boot --offlineauto=off",
            "/usr/local/sbin/smartctl -d megaraid,12 -x /dev/da0 --saveauto=off",
            "/usr/sbin/smartctl -x -j /dev/../etc/shadow",
        )
        grants = tuple(
            grant
            for platform in self.platforms
            for grant in SUDO_COMMANDS_BY_PLATFORM[platform]
            if Path(shlex.split(grant)[0]).name == "smartctl"
        )
        for command in legitimate:
            with self.subTest(command=command):
                self.assertTrue(any(sudoers_grant_matches(grant, command) for grant in grants))
        for command in rejected:
            with self.subTest(command=command):
                self.assertFalse(any(sudoers_grant_matches(grant, command) for grant in grants))

    def test_core_regex_grants_hold_nothing_the_middleware_escapes_inside_a_regex(self) -> None:
        for source, grants in core_grant_sources().items():
            for grant in grants:
                arguments = grant.partition(" ")[2]
                if not (arguments.startswith("^") and arguments.endswith("$")):
                    continue
                with self.subTest(source=source, grant=grant):
                    self.assertNotIn("\\", grant)
                    self.assertNotIn(":", grant)
                    # Literal braces in bracket expressions are fine; an
                    # interval's comma would be stored as \\, (#943).
                    self.assertNotRegex(
                        arguments.replace("[{]", "").replace("[}]", ""), r"[{}]",
                        "the comma of an interval is stored as \\,",
                    )
                    for expression in re.findall(r"\[\]?[^\]]*\]", arguments):
                        self.assertNotIn(",", expression)

    def test_core_smart_grants_still_match_after_middleware_escaping(self) -> None:
        for source, grants in core_grant_sources().items():
            for command in CORE_SMART_ALLOWED:
                with self.subTest(source=source, allowed=command):
                    self.assertTrue(any(core_stored_grant_matches(grant, command) for grant in grants))
            for command in CORE_SMART_REFUSED:
                with self.subTest(source=source, refused=command):
                    self.assertFalse(any(core_stored_grant_matches(grant, command) for grant in grants))

    def test_core_smart_grants_name_only_the_core_binary_without_device_types(self) -> None:
        for source, grants in core_grant_sources().items():
            smart = [grant for grant in grants if Path(grant.split()[0]).name == "smartctl"]
            with self.subTest(source=source):
                self.assertTrue(smart)
                self.assertTrue(all(grant.startswith("/usr/local/sbin/smartctl ^-x ") for grant in smart), smart)
        # File-based platforms keep the shared form; visudo parses it there.
        for platform in ("scale", "linux", "quantastor"):
            with self.subTest(platform=platform):
                for grant in SMARTCTL_SUDO_COMMANDS:
                    self.assertIn(grant, SUDO_COMMANDS_BY_PLATFORM[platform])

    def test_quantastor_bootstrap_does_not_grant_unbounded_root_cli_access(self) -> None:
        self.assertFalse(
            any(
                grant.startswith("/usr/bin/qs")
                for grant in SUDO_COMMANDS_BY_PLATFORM["quantastor"]
            )
        )


class CoreExpanderCounterGrantTests(unittest.TestCase):
    """#941: one sudo rule for the REPORT PHY ERROR LOG command shape, granted by default."""

    def grant(self):
        from app.services.core_expander_counters import SMP_PHY_ERROR_LOG_SUDO_GRANT

        return SMP_PHY_ERROR_LOG_SUDO_GRANT

    def matches(self, command: str) -> bool:
        # The CORE middleware form, plus the plain sudoers model for the same rule.
        stored = core_stored_grant_matches(self.grant(), command)
        self.assertEqual(stored, sudoers_grant_matches(self.grant(), command), command)
        return stored

    def test_every_core_grant_source_has_exactly_one_smp_rule(self):
        for source, grants in core_grant_sources().items():
            with self.subTest(source=source):
                smp = [grant for grant in grants if grant.startswith("/sbin/camcontrol ") and "smp" in grant]
                self.assertEqual(smp, [self.grant()])
                self.assertNotIn("smpphylist", " ".join(grants))
        for platform in ("scale", "linux", "quantastor", "esxi"):
            for source in (SUDO_COMMANDS_BY_PLATFORM, SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM):
                with self.subTest(platform=platform):
                    self.assertNotIn(self.grant(), source[platform])

    def test_custom_command_lists_still_receive_the_rule(self):
        grants = ServiceAccountBootstrapService._resolve_sudo_commands(
            "core", ["sudo -n /usr/sbin/sesutil map"],
        )
        self.assertIn(self.grant(), grants)

    def test_rule_matches_every_phy_on_any_pass_device(self):
        from app.services.core_expander_counters import phy_error_log_argv

        for device in ("pass0", "pass3", "pass12", "pass4095"):
            for phy in range(255):
                with self.subTest(device=device, phy=phy):
                    self.assertTrue(self.matches(shlex.join(phy_error_log_argv(device, phy))))

    def test_rule_survives_the_core_middleware_unchanged(self):
        self.assertEqual(middleware_stored_grant(self.grant()), self.grant())
        # The generated wiki policy block embeds it; `](` would read as a link.
        self.assertNotIn("](", self.grant())
        self.assertTrue(self.grant().startswith("/sbin/camcontrol ^smpcmd "))
        self.assertTrue(self.grant().endswith(" -R 28 -$"))

    def test_rule_refuses_every_other_function_length_option_and_shell_form(self):
        from app.services.core_expander_counters import phy_error_log_argv

        good = phy_error_log_argv("pass3", 0x2A)
        fmt = good[5]
        fields = fmt.split("{}")

        def with_field(index: int, value: str) -> str:
            changed = list(fields)
            changed[index] = value
            return "{}".join(changed)

        refused = [
            # The 255th PHY identifier is outside the admitted range.
            (*good[:5], with_field(9, "ff"), *good[6:]),
            # Other camcontrol SMP commands, including PHY control and resets.
            ("/sbin/camcontrol", "smppc", "pass3", "-p", "3", "-o", "linkreset"),
            ("/sbin/camcontrol", "smppc", "pass3", "-p", "3", "-o", "hardreset"),
            ("/sbin/camcontrol", "smppc", "pass3", "-p", "3", "-o", "disable"),
            ("/sbin/camcontrol", "smppc", "pass3", "-p", "3", "-o", "clearerrlog"),
            ("/sbin/camcontrol", "smprg", "pass3"),
            ("/sbin/camcontrol", "smprg", "pass3", "-l"),
            ("/sbin/camcontrol", "smpphylist", "pass3"),
            ("/sbin/camcontrol", "smpmaninfo", "pass3"),
            ("/sbin/camcontrol", "reset", "pass3"),
            ("/sbin/camcontrol", "smpcmd", "pass3", "-r", "12", "-", "-R", "28", "-"),
            # Other request or response lengths.
            (*good[:4], "16", *good[5:]),
            (*good[:4], "44", *good[5:]),
            (*good[:7], "32", "-"),
            (*good[:7], "1024", "-"),
            (*good[:7], "28", "b4"),
            (*good[:5], fmt + "{}00", *good[6:]),
            (*good[:5], "{}".join(fields[:-1]), *good[6:]),
            # Extra, moved or inserted arguments and options.
            (*good, "-v"),
            (*good, "-r", "12", "-"),
            (*good[:3], "-v", *good[3:]),
            (*good[:3], "-t", "60", *good[3:]),
            (*good[:3], "-C", "9", *good[3:]),
            ("/sbin/camcontrol", "smpcmd", "pass3", *good[6:], *good[3:6]),
            # Other device names and spellings of the request.
            (*good[:2], "/dev/pass3", *good[3:]),
            (*good[:2], "pass", *good[3:]),
            (*good[:2], "da3", *good[3:]),
            (*good[:2], "ses0", *good[3:]),
            (*good[:2], "pass3x", *good[3:]),
            (*good[:5], fmt.upper(), *good[6:]),
            (*good[:5], " ".join(fields), *good[6:]),
            (*good[:5], fmt.replace("{}", "{} "), *good[6:]),
            ("/sbin/../sbin/camcontrol", *good[1:]),
            ("/usr/sbin/camcontrol", *good[1:]),
            # Shell metacharacters never reach a matching argument list.
            (*good, ";", "reset"),
            (*good, "&&", "true"),
            (*good, "|", "sh"),
            (*good, ">", "/tmp/out"),
            (*good[:2], "pass3;reboot", *good[3:]),
            (*good[:2], "$(reboot)", *good[3:]),
            (*good[:2], "`reboot`", *good[3:]),
            (*good[:2], "pass3\nreset", *good[3:]),
        ]
        # Every fixed request byte, including function, allocated length and
        # request length, admits only its one value.
        for index, value in enumerate(fields):
            if index == 9:
                continue
            for other in ("91", "10", "00" if value != "00" else "01", "81", "ff"):
                if other != value:
                    refused.append((*good[:5], with_field(index, other), *good[6:]))
        for argv in refused:
            with self.subTest(argv=argv):
                self.assertFalse(self.matches(shlex.join(argv)))

    def test_resplit_arguments_that_match_never_send_another_request(self):
        """sudo matches argv joined by spaces; model every re-split that still matches.

        Mirrors FreeBSD 13 camcontrol smpcmd(): BSD getopt over "r:R:" stops at
        the first non-option, `-r len fmt` and `-R len -` take the format as the
        next argument, a format starting with '-' means stdin, and nothing is
        sent unless both -r and -R were parsed.
        """
        from app.services.core_expander_counters import phy_error_log_argv

        canonical = phy_error_log_argv("pass3", 5)
        words = canonical[1:]
        sent = 0
        for mask in range(1 << (len(words) - 1)):
            tokens = [words[0]]
            for index, word in enumerate(words[1:]):
                if mask & (1 << index):
                    tokens[-1] = f"{tokens[-1]} {word}"
                else:
                    tokens.append(word)
            argv = (canonical[0], *tokens)
            self.assertTrue(self.matches(shlex.join(argv)), argv)
            outcome = self._model_smpcmd(tokens)
            if outcome is None:
                continue
            sent += 1
            self.assertEqual(outcome, ("format", canonical[5], 12, 28), argv)
        self.assertGreaterEqual(sent, 1)

    @staticmethod
    def _strtol(text: str) -> int:
        match = re.match(r"\s*([0-9]+)", text)
        return int(match.group(1)) if match else 0

    def _model_smpcmd(self, tokens: list[str]):
        if tokens[0] != "smpcmd" or re.fullmatch(r"pass[0-9]+", tokens[1]) is None:
            return None  # unknown command or unusable device: nothing is sent
        args = tokens[2:]
        index = 0
        request = response = None
        while index < len(args):
            arg = args[index]
            if not arg.startswith("-") or arg == "-" or len(arg) < 2 or arg[1] not in "rR":
                break  # BSD getopt stops at the first non-option
            if len(arg) > 2:
                optarg, index = arg[2:], index + 1
            elif index + 1 < len(args):
                optarg, index = args[index + 1], index + 2
            else:
                return None
            size = self._strtol(optarg)
            if size <= 0:
                return None
            datastr = args[index] if index < len(args) else None
            if datastr is not None:
                index += 1
            if arg[1] == "r":
                request = ("stdin" if datastr and datastr.startswith("-") else "format", datastr, size)
            else:
                response = size
        if request is None or response is None:
            return None  # "need both the request (-r) and response (-R) arguments"
        return (*request, response)

    def test_argv_builder_has_only_the_exact_report_phy_error_log_bytes(self):
        from app.services.core_expander_counters import phy_error_log_argv

        for phy in (0, 3, 127, 254):
            with self.subTest(phy=phy):
                argv = phy_error_log_argv("pass3", phy)
                request = bytes.fromhex(" ".join(argv[5].split("{}")))
                self.assertEqual(request, bytes((0x40, 0x11, 6, 2, 0, 0, 0, 0, 0, phy, 0, 0)))
                self.assertEqual(argv[:5], ("/sbin/camcontrol", "smpcmd", "pass3", "-r", "12"))
                self.assertEqual(argv[6:], ("-R", "28", "-"))

    def test_builder_rejects_bad_phy_and_device_instead_of_normalizing(self):
        from app.services.core_expander_counters import phy_error_log_argv

        for phy in (-1, 255, True, "3", None, 3.0):
            with self.subTest(phy=phy), self.assertRaises(ValueError):
                phy_error_log_argv("pass3", phy)
        for device in ("pass3 -v", "../pass3", "/dev/pass3", "pass3\x00", "pass３", "ses3"):
            with self.subTest(device=device), self.assertRaises(ValueError):
                phy_error_log_argv(device, 3)


def _phy_error_log_frame(
    phy: int = 5,
    counters: tuple[int, int, int, int] = (1, 2, 3, 4),
    *,
    frame_type: int = 0x41,
    function: int = 0x11,
    result: int = 0,
    response_length: int = 6,
    change_count: int = 0x0102,
) -> bytes:
    """Synthetic SPL-4 Table 336 response, CRC excluded as camcontrol reports it."""
    header = bytes((frame_type, function, result, response_length))
    body = change_count.to_bytes(2, "big") + bytes(3) + bytes((phy,)) + bytes(2)
    return header + body + b"".join(value.to_bytes(4, "big") for value in counters)


class CoreExpanderCounterDecoderTests(unittest.TestCase):
    """Pure decoder/delta draft: synthetic byte frames only, never wired or run."""

    counter_fields = (
        "invalid_dword_count", "running_disparity_error_count",
        "loss_of_dword_synchronization_count", "phy_reset_problem_count",
    )

    def decode(self, frame, phy=5):
        from app.services.core_expander_counters import decode_phy_error_log_response

        return decode_phy_error_log_response(frame, phy=phy)

    def assert_frame_error(self, frame, reason, phy=5):
        from app.services.core_expander_counters import SmpResponseError

        with self.assertRaises(SmpResponseError) as caught:
            self.decode(frame, phy=phy)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def row(self, phy, *counters):
        return {"phy": phy, **dict(zip(self.counter_fields, counters))}

    def deltas(self, previous, current):
        from app.services.core_expander_counters import phy_counter_deltas

        return phy_counter_deltas(previous, current)

    def test_valid_frame_decodes_big_endian_counters_in_per_phy_shape(self):
        frame = _phy_error_log_frame(7, (0x01020304, 0, 0x00A0B0C0, 9))
        self.assertEqual(len(frame), 28)
        for value in (frame, bytearray(frame), memoryview(frame)):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(
                    self.decode(value, phy=7),
                    self.row(7, 0x01020304, 0, 0x00A0B0C0, 9),
                )

    def test_measured_zero_is_kept_and_saturation_is_not_wrapped(self):
        self.assertEqual(self.decode(_phy_error_log_frame(5, (0, 0, 0, 0))), self.row(5, 0, 0, 0, 0))
        saturated = self.decode(_phy_error_log_frame(5, (0xFFFFFFFF, 1, 0xFFFFFFFF, 0)))
        self.assertEqual(saturated, self.row(5, 4294967295, 1, 4294967295, 0))

    def test_short_and_long_frames_raise_instead_of_returning_zeros(self):
        frame = _phy_error_log_frame()
        for size in (0, 3, 4, 12, 27):
            with self.subTest(size=size):
                self.assert_frame_error(frame[:size], "frame_length")
        # 29 bytes and the CRC-inclusive 32-byte transport frame are both refused.
        for extra in (b"\x00", bytes(4)):
            with self.subTest(extra=len(extra)):
                self.assert_frame_error(frame + extra, "frame_length")

    def test_wrong_frame_type_or_function_code_raises(self):
        for frame_type in (0x40, 0x00, 0xFF):
            with self.subTest(frame_type=frame_type):
                self.assert_frame_error(_phy_error_log_frame(frame_type=frame_type), "frame_type")
        # 10h is DISCOVER and 91h is PHY CONTROL; neither is accepted as an error log.
        for function in (0x10, 0x91, 0x00, 0x13):
            with self.subTest(function=function):
                self.assert_frame_error(_phy_error_log_frame(function=function), "function")

    def test_non_zero_function_result_raises_even_for_truncated_error_frames(self):
        for result in (0x01, 0x10, 0x11, 0x16):
            with self.subTest(result=result):
                error = self.assert_frame_error(_phy_error_log_frame(result=result), "function_result")
                self.assertEqual(error.function_result, result)
                error = self.assert_frame_error(bytes((0x41, 0x11, result, 0)), "function_result")
                self.assertEqual(error.function_result, result)

    def test_bad_response_length_field_raises(self):
        # SAS-1.1 zero lengths are not admitted; SAS-2+ reports exactly 06h dwords.
        for response_length in (0, 5, 7, 0xFF):
            with self.subTest(response_length=response_length):
                self.assert_frame_error(_phy_error_log_frame(response_length=response_length), "response_length")

    def test_phy_identifier_must_match_the_requested_phy(self):
        self.assert_frame_error(_phy_error_log_frame(phy=6), "phy_identifier", phy=5)
        from app.services.core_expander_counters import decode_phy_error_log_response

        for phy in (-1, 255, True, "5", None, 5.0):
            with self.subTest(phy=phy), self.assertRaises(ValueError):
                decode_phy_error_log_response(_phy_error_log_frame(), phy=phy)

    def test_non_byte_inputs_are_rejected(self):
        frame = _phy_error_log_frame()
        for value in (frame.hex(), list(frame), None, 28):
            with self.subTest(kind=type(value).__name__):
                self.assert_frame_error(value, "frame_type_error")

    def test_normal_delta_counts_increments_per_phy(self):
        previous = [self.row(1, 10, 20, 30, 40), self.row(0, 0, 0, 0, 0)]
        current = [self.row(0, 3, 0, 1, 0), self.row(1, 15, 20, 31, 40)]
        rows = self.deltas(previous, current)
        self.assertEqual([row["phy"] for row in rows], [0, 1])
        self.assertEqual({key: rows[1][key] for key in self.counter_fields}, dict(zip(self.counter_fields, (5, 0, 1, 0))))
        self.assertEqual({key: rows[0][key] for key in self.counter_fields}, dict(zip(self.counter_fields, (3, 0, 1, 0))))
        for row in rows:
            self.assertEqual(set(row), {"phy", "counter_states", *self.counter_fields})
            self.assertEqual(row["counter_states"], dict.fromkeys(self.counter_fields, "delta"))

    def test_decrease_is_a_counter_reset_never_a_negative_delta(self):
        rows = self.deltas([self.row(2, 100, 5, 0xFFFFFFFF, 7)], [self.row(2, 3, 0, 9, 7)])
        self.assertEqual({key: rows[0][key] for key in self.counter_fields}, dict(zip(self.counter_fields, (3, 0, 9, 0))))
        self.assertEqual(
            rows[0]["counter_states"],
            dict(zip(self.counter_fields, ("reset", "reset", "reset", "delta"))),
        )
        self.assertTrue(all(rows[0][key] >= 0 for key in self.counter_fields))

    def test_saturated_counter_reports_unknown_delta_not_a_number(self):
        rows = self.deltas(
            [self.row(4, 0xFFFFFFFF, 10, 0, 0)],
            [self.row(4, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0)],
        )
        self.assertIsNone(rows[0]["invalid_dword_count"])
        self.assertIsNone(rows[0]["running_disparity_error_count"])
        self.assertEqual(rows[0]["counter_states"]["invalid_dword_count"], "saturated")
        self.assertEqual(rows[0]["counter_states"]["running_disparity_error_count"], "saturated")
        self.assertEqual(rows[0]["loss_of_dword_synchronization_count"], 0)

    def test_missing_values_and_new_phys_have_no_numeric_delta(self):
        rows = self.deltas(
            [self.row(0, None, 1, 1, 1), self.row(9, 1, 1, 1, 1)],
            [self.row(0, 5, None, 1, 1), self.row(3, 0, 0, 0, 0)],
        )
        self.assertEqual([row["phy"] for row in rows], [0, 3])
        self.assertEqual(
            (rows[0]["invalid_dword_count"], rows[0]["running_disparity_error_count"]), (None, None),
        )
        self.assertEqual(rows[0]["counter_states"]["invalid_dword_count"], "no_baseline")
        self.assertEqual(rows[0]["counter_states"]["running_disparity_error_count"], "unavailable")
        self.assertEqual(rows[1]["counter_states"], dict.fromkeys(self.counter_fields, "no_baseline"))
        self.assertTrue(all(rows[1][key] is None for key in self.counter_fields))

    def test_malformed_samples_raise_a_typed_error(self):
        from app.services.core_expander_counters import CounterSampleError

        good = self.row(0, 1, 2, 3, 4)
        malformed = [
            [good, dict(good)],
            [{**good, "invalid_dword_count": True}],
            [{**good, "invalid_dword_count": -1}],
            [{**good, "invalid_dword_count": 0x100000000}],
            [{**good, "invalid_dword_count": 1.0}],
            [{**good, "invalid_dword_count": "1"}],
            [{key: value for key, value in good.items() if key != "phy_reset_problem_count"}],
            [{**good, "phy": True}],
            [{**good, "phy": 256}],
            ["phy 0"],
            {"phy": 0},
            None,
        ]
        for sample in malformed:
            for previous, current in ((sample, [good]), ([good], sample)):
                with self.subTest(sample=sample), self.assertRaises(CounterSampleError) as caught:
                    self.deltas(previous, current)
                self.assertIsInstance(caught.exception, ValueError)

    def test_decoded_rows_match_the_linux_sysfs_counter_shape(self):
        from app.models.domain import SasPortView
        from app.services.core_expander_counters import COUNTER_FIELDS, SYSFS_COUNTER_ATTRIBUTES

        self.assertEqual(COUNTER_FIELDS, self.counter_fields)
        self.assertEqual(set(COUNTER_FIELDS) - set(SasPortView.model_fields), set())
        # Linux scsi_transport_sas exports these sas_phy attributes as "%u\n".
        self.assertEqual(
            set(SYSFS_COUNTER_ATTRIBUTES),
            {"invalid_dword_count", "running_disparity_error_count", "loss_of_dword_sync_count", "phy_reset_problem_count"},
        )
        self.assertEqual(tuple(SYSFS_COUNTER_ATTRIBUTES.values()), COUNTER_FIELDS)
        sysfs_fixture = {
            "phy_identifier": "5\n", "invalid_dword_count": "12\n", "running_disparity_error_count": "0\n",
            "loss_of_dword_sync_count": "3\n", "phy_reset_problem_count": "4294967295\n",
        }
        sysfs_row = {"phy": int(sysfs_fixture["phy_identifier"])}
        sysfs_row.update({field: int(sysfs_fixture[attribute]) for attribute, field in SYSFS_COUNTER_ATTRIBUTES.items()})
        decoded = self.decode(_phy_error_log_frame(5, (12, 0, 3, 0xFFFFFFFF)))
        self.assertEqual(decoded, sysfs_row)
        self.assertEqual(SasPortView(port=0, **decoded).model_dump(include=set(decoded) - {"phy"}),
                         {key: decoded[key] for key in COUNTER_FIELDS})
        # Either source feeds the same delta helper and yields identical rows.
        later = self.decode(_phy_error_log_frame(5, (15, 0, 3, 0xFFFFFFFF)))
        self.assertEqual(self.deltas([sysfs_row], [later]), self.deltas([decoded], [later]))

    def test_decoder_and_delta_perform_no_io(self):
        frame = _phy_error_log_frame()
        with patch("builtins.open", side_effect=AssertionError("file IO")), \
                patch("subprocess.run", side_effect=AssertionError("exec")), \
                patch("socket.create_connection", side_effect=AssertionError("network")):
            decoded = self.decode(frame)
            self.deltas([decoded], [decoded])


class BootstrapPolicyAdmissionTests(unittest.TestCase):
    """Policy admission uses only inline synthetic inputs and a recording probe."""

    platforms = ("core", "scale", "linux", "quantastor")
    custom_commands = ["sudo -n /usr/bin/id", "sudo /usr/bin/id"]

    def setUp(self) -> None:
        FakeProbe.last_config = None
        FakeProbe.last_command = None
        FakeProbe.last_stdin = None
        self.probe_calls = []

        def recording_factory(config):
            self.probe_calls.append(config)
            return cast(SSHProbe, FakeProbe(config))

        self.service = ServiceAccountBootstrapService(
            "synthetic-config.yaml", probe_factory=recording_factory
        )

    @staticmethod
    def payload(platform, *, commands=(), install=True):
        return SystemSetupBootstrapRequest(
            platform=platform,
            host="host.example.test",
            bootstrap_user="root",
            bootstrap_key_path="unused-bootstrap-key",
            service_public_key="ssh-ed25519 placeholder",
            sudo_commands=list(commands),
            install_sudo_rules=install,
        )

    def assert_bootstrap_refused(self, payload, message):
        self.probe_calls.clear()
        with patch.object(
            self.service, "_resolve_public_key", wraps=self.service._resolve_public_key
        ) as key_lookup:
            error = None
            try:
                self.service.bootstrap_service_account(payload)
            except ValueError as exc:
                error = str(exc)
            self.assertEqual(self.probe_calls, [], "Rejected policy must not create a probe")
            key_lookup.assert_not_called()
            self.assertIsNotNone(error, "Rejected policy must raise ValueError")
            assert isinstance(error, str)
            self.assertRegex(error, message)

    def test_ipmi_bootstrap_refuses_before_key_lookup_or_probe(self):
        for install in (True, False):
            for commands in ([], [" ", "sudo -n"], self.custom_commands):
                with self.subTest(install=install, commands=commands):
                    self.assert_bootstrap_refused(
                        self.payload("ipmi", commands=commands, install=install),
                        "BMC/IPMI.*bootstrap",
                    )

    def test_ipmi_preview_is_disabled_even_with_custom_commands(self):
        for install in (True, False):
            for commands in ([], self.custom_commands):
                with self.subTest(install=install, commands=commands):
                    preview = self.service.build_sudoers_preview(
                        "jbodmap", "ipmi", install_sudo_rules=install,
                        requested_commands=commands,
                    )
                    self.assertFalse(preview["enabled"])
                    assert isinstance(preview["detail"], str)
                    assert isinstance(preview["content"], str)
                    self.assertRegex(preview["detail"], "BMC/IPMI.*bootstrap")
                    self.assertNotIn("NOPASSWD:", preview["content"])
                    self.assertEqual(self.probe_calls, [])

    def test_unsupported_platform_cannot_render_a_policy(self):
        for platform in ("ipmi", "unsupported"):
            for commands in (None, [], self.custom_commands):
                with self.subTest(platform=platform, commands=commands):
                    with self.assertRaises(ValueError):
                        self.service._build_sudoers_content("jbodmap", platform, commands)

    def test_empty_effective_policy_refuses_execution_and_preview(self):
        for platform in self.platforms:
            for defaults in ((), ("", " ")):
                for commands in ([], [" ", "sudo -n", "/usr/bin/id"]):
                    with self.subTest(platform=platform, defaults=defaults, commands=commands):
                        with patch.dict(SUDO_COMMANDS_BY_PLATFORM, {platform: defaults}):
                            self.assert_bootstrap_refused(
                                self.payload(platform, commands=commands),
                                "No command-limited sudo policy",
                            )
                            with self.assertRaisesRegex(ValueError, "No command-limited sudo policy"):
                                self.service.build_sudoers_preview(
                                    "jbodmap", platform, requested_commands=commands
                                )

    def test_core_empty_policy_cannot_reach_the_middleware_payload(self):
        with patch.dict(SUDO_COMMANDS_BY_PLATFORM, {"core": ()}):
            with self.assertRaisesRegex(ValueError, "No command-limited sudo policy"):
                self.service._build_core_midclt_user_update_payload([])

    def assert_preview_matches_recorded_script(self, platform, commands):
        preview = self.service.build_sudoers_preview(
            "jbodmap", platform, requested_commands=commands
        )
        self.assertTrue(preview["enabled"])
        assert isinstance(preview["content"], str)
        self.assertNotIn("NOPASSWD: ALL", preview["content"])
        result = self.service.bootstrap_service_account(self.payload(platform, commands=commands))
        self.assertTrue(result["ok"])
        self.assertEqual(FakeProbe.last_command, "/bin/sh -s")
        assert isinstance(FakeProbe.last_stdin, str)
        if platform == "core":
            policy = shlex.split(preview["content"])[-1]
            self.assertIn("midclt_payload=" + shlex.quote(policy), FakeProbe.last_stdin)
            decoded = json.loads(policy)
            self.assertTrue(decoded["sudo"])
            self.assertTrue(decoded["sudo_nopasswd"])
            self.assertEqual(
                decoded["sudo_commands"],
                list(self.service._resolve_sudo_commands(platform, commands)),
            )
        else:
            self.assertIn(preview["content"], FakeProbe.last_stdin)
            self.assertIn("Cmnd_Alias JBODMAP_" + platform.upper() + "_CMDS", preview["content"])

    def test_supported_defaults_match_preview_and_recorded_execution(self):
        for platform in self.platforms:
            for commands in ([], [" ", "sudo -n", "/usr/bin/id"]):
                with self.subTest(platform=platform, commands=commands):
                    self.assertEqual(
                        self.service._resolve_sudo_commands(platform, commands),
                        SUDO_COMMANDS_BY_PLATFORM[platform],
                    )
                    self.assert_preview_matches_recorded_script(platform, commands)

    def test_custom_commands_keep_normalization_supplements_and_preview_parity(self):
        for platform in self.platforms:
            with self.subTest(platform=platform):
                self.assertEqual(
                    self.service._resolve_sudo_commands(platform, self.custom_commands),
                    ("/usr/bin/id", *SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM[platform]),
                )
                self.assert_preview_matches_recorded_script(platform, self.custom_commands)
                # A nonempty custom policy does not require platform defaults.
                with patch.dict(SUDO_COMMANDS_BY_PLATFORM, {platform: ()}):
                    self.assert_preview_matches_recorded_script(platform, self.custom_commands)

    def test_disabled_supported_policy_does_not_require_commands(self):
        for platform in self.platforms:
            with self.subTest(platform=platform):
                with patch.dict(SUDO_COMMANDS_BY_PLATFORM, {platform: ()}):
                    preview = self.service.build_sudoers_preview(
                        "jbodmap", platform, install_sudo_rules=False
                    )
                    result = self.service.bootstrap_service_account(self.payload(platform, install=False))
                self.assertFalse(preview["enabled"])
                self.assertTrue(result["ok"])
                self.assertFalse(result["sudo_rules_installed"])
                assert isinstance(FakeProbe.last_stdin, str)
                self.assertIn("install_sudo=0", FakeProbe.last_stdin)
                self.assertNotIn("NOPASSWD:", FakeProbe.last_stdin)
                if platform == "core":
                    self.assertIn("midclt_payload=''", FakeProbe.last_stdin)
                    self.assertNotIn('"sudo_commands":', FakeProbe.last_stdin)

    def test_esxi_still_refuses_bootstrap_and_disables_preview(self):
        for install in (True, False):
            with self.subTest(install=install):
                self.assert_bootstrap_refused(
                    self.payload("esxi", install=install), "VMware ESXi"
                )
                preview = self.service.build_sudoers_preview(
                    "jbodmap", "esxi", install_sudo_rules=install
                )
                self.assertFalse(preview["enabled"])
                assert isinstance(preview["detail"], str)
                self.assertIn("VMware ESXi", preview["detail"])

    def test_linux_policy_retains_syntax_and_active_include_checks(self):
        self.service.bootstrap_service_account(self.payload("linux"))
        script = FakeProbe.last_stdin
        assert isinstance(script, str)
        for required in (
            "unsupported include syntax or continued policy lines.",
            "no supported active includedir was found.",
            "timeout 10 visudo -c >",
            'timeout 10 visudo -cf "$temp_sudoers_path"',
            'grep -Fxq "$sudoers_path: parsed OK"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)


if __name__ == "__main__":
    unittest.main()
