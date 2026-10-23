from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization

from app.config import Settings, SSHConfig, _derive_runtime_layout_paths, normalize_text
from app.models.domain import SystemSetupBootstrapRequest
from app.services.ssh_key_manager import SSHKeyManager
from app.services.ssh_probe import SSHProbe, redact_ssh_command


PUBLIC_KEY_PREFIXES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-", "sk-ssh-")
SUDOERS_DIR_CANDIDATES = ("/usr/local/etc/sudoers.d", "/etc/sudoers.d")
SAVED_SUDO_COMMAND_PREFIX = re.compile(r"^sudo\b", re.IGNORECASE)
SAVED_SSH_COMMANDS_UNAVAILABLE_DETAIL = "The saved SSH command list is unavailable."
BOOTSTRAP_SUDO_PASSWORD_UNSUPPORTED_DETAIL = (
    "Bootstrap sends its setup script to the host as command input, so it cannot also "
    "send a sudo password. Log in as root, or as a user with passwordless sudo, and "
    "leave the sudo password blank."
)
CORE_SERVICE_HOME_HELP = (
    "with a home directory under /mnt, shell sh, Disable Password on and this app's "
    "SSH public key pasted in"
)
CORE_SERVICE_HOME_EXAMPLE = "/mnt/tank/home/jbodmap"
CORE_SERVICE_HOME_FIELD_HELP = (
    f"fill in Home directory with a folder on a pool, for example {CORE_SERVICE_HOME_EXAMPLE}, "
    "and setup creates the account"
)
CORE_SERVICE_HOME_INVALID_DETAIL = (
    f"Home directory must be a folder on a pool, for example {CORE_SERVICE_HOME_EXAMPLE}. "
    "Use a path under /mnt/<pool>/ made of letters, digits, '_', '.', '-' and '/' only, "
    "with no '.' or '..' parts."
)
CORE_SERVICE_HOME_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
CORE_SERVICE_ACCOUNT_FULL_NAME = "TrueNAS JBOD UI service account"
ESXI_BOOTSTRAP_UNSUPPORTED_DETAIL = (
    "VMware ESXi does not use the Linux one-time bootstrap or sudoers flow. "
    "Save the SSH host, root or key-based auth, and the read-only runtime commands directly instead."
)
CORE_MPRUTIL_SUDO_COMMANDS = (
    "/usr/sbin/mprutil show adapter",
    "/usr/sbin/mprutil show adapters",
    "/usr/sbin/mprutil show all",
    "/usr/sbin/mprutil show devices",
    "/usr/sbin/mprutil show enclosures",
    "/usr/sbin/mprutil show expanders",
    "/usr/sbin/mprutil show iocfacts",
    # Anchored, backslash-free EREs (sudo 1.9.10+). A `-u *` glob let sudo
    # match any argument list that ended in `show <page>`.
    "/usr/sbin/mprutil ^-u [0-9]+ show adapter$",
    "/usr/sbin/mprutil ^-u [0-9]+ show all$",
    "/usr/sbin/mprutil ^-u [0-9]+ show devices$",
    "/usr/sbin/mprutil ^-u [0-9]+ show enclosures$",
    "/usr/sbin/mprutil ^-u [0-9]+ show expanders$",
    "/usr/sbin/mprutil ^-u [0-9]+ show iocfacts$",
)
CORE_SESUTIL_LOCATE_SUDO_COMMANDS = (
    "/usr/sbin/sesutil ^locate -u /dev/ses[0-9]+ [0-9]+ on$",
    "/usr/sbin/sesutil ^locate -u /dev/ses[0-9]+ [0-9]+ off$",
)
CORE_DMIDECODE_SUDO_COMMANDS = (
    "/usr/local/sbin/dmidecode -t slot",
)
CORE_MESSAGES_SUDO_COMMANDS = (
    "/usr/bin/tail -n 4000 /var/log/messages",
)
# Device-specific probes the collection path builds at run time from discovered
# devices. They never appear in an operator's saved command list, so the bootstrap
# has to grant them in both the seeded and the supplemental flow (#332, #335).
LINUX_SG_SES_SUDO_COMMANDS = (
    "/usr/bin/sg_ses ^-p aes /dev/sg[0-9]+$",
    "/usr/bin/sg_ses ^-p ec /dev/sg[0-9]+$",
    "/usr/bin/sg_ses ^--join --filter /dev/sg[0-9]+$",
    "/usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --set=ident /dev/sg[0-9]+$",
    "/usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --clear=ident /dev/sg[0-9]+$",
)
SMARTCTL_SUDO_COMMANDS = (
    "/usr/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
    "/usr/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
    r"/usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
    r"/usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
    "/usr/local/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
    "/usr/local/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
    r"/usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
    r"/usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$",
)
# TrueNAS CORE stores grants through the middleware, whose sudoers template
# escapes `\`, `,`, `:` and `=` with a backslash. Inside a regex that turns the
# interval `{0,2}` into `{0\,2}`, which matches nothing (#943). These forms use
# two optional path groups instead and keep `:` out of the bracket expressions.
# CORE installs smartctl only under /usr/local/sbin, the middleware refuses a
# grant naming a missing executable, and CORE disks never carry a `-d` type.
CORE_SMARTCTL_DEVICE_PATTERN = (
    "/dev/[A-Za-z0-9_+-][A-Za-z0-9_.+-]*"
    "(/[A-Za-z0-9_+-][A-Za-z0-9_.+-]*)?"
    "(/[A-Za-z0-9_+-][A-Za-z0-9_.+-]*)?"
)
CORE_SMARTCTL_SUDO_COMMANDS = (
    f"/usr/local/sbin/smartctl ^-x -j {CORE_SMARTCTL_DEVICE_PATTERN}$",
    f"/usr/local/sbin/smartctl ^-x {CORE_SMARTCTL_DEVICE_PATTERN}$",
)
LINUX_NVME_SUDO_COMMANDS = (
    "/usr/sbin/nvme ^smart-log -o json /dev/nvme[0-9]+$",
    "/usr/sbin/nvme ^id-ctrl -o json /dev/nvme[0-9]+$",
    "/usr/sbin/nvme ^id-ns -o json /dev/nvme[0-9]+n[0-9]+$",
)
CORE_MPRUTIL_SHOW_COMMANDS = {
    "adapter",
    "adapters",
    "all",
    "devices",
    "enclosures",
    "expanders",
    "iocfacts",
}
CORE_MPRUTIL_UNIT_SHOW_COMMANDS = CORE_MPRUTIL_SHOW_COMMANDS - {"adapters"}
# TrueNAS middleware calls behind the main UI's disk inventory sync action (#357).
# Backslash-free anchored POSIX EREs survive TrueNAS sudoers rendering and admit
# only the numeric-ID filter built by the poll loop. Sudo globs can otherwise
# consume whitespace and additional arguments.
CORE_MIDCLT_DISK_SYNC_SUDO_COMMANDS = (
    "/usr/local/bin/midclt call disk.multipath_sync",
    "/usr/local/bin/midclt call disk.sync_all",
    r'/usr/local/bin/midclt ^call core[.]get_jobs [[][[]"id","=",[0-9]+[]][]]$',
)
SCALE_MIDCLT_DISK_SYNC_SUDO_COMMANDS = (
    "/usr/bin/midclt call disk.sync_all",
    r'/usr/bin/midclt ^call core[.]get_jobs [[][[]"id","=",[0-9]+[]][]]$',
)
SUDO_COMMANDS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "core": (
        "/usr/sbin/sesutil map",
        "/usr/sbin/sesutil show",
        *CORE_SESUTIL_LOCATE_SUDO_COMMANDS,
        "/sbin/camcontrol devlist -v",
        *CORE_SMARTCTL_SUDO_COMMANDS,
        *CORE_MPRUTIL_SUDO_COMMANDS,
        *CORE_DMIDECODE_SUDO_COMMANDS,
        *CORE_MESSAGES_SUDO_COMMANDS,
        *CORE_MIDCLT_DISK_SYNC_SUDO_COMMANDS,
    ),
    "scale": (
        *LINUX_SG_SES_SUDO_COMMANDS,
        *SMARTCTL_SUDO_COMMANDS,
        *SCALE_MIDCLT_DISK_SYNC_SUDO_COMMANDS,
    ),
    "linux": (
        "/usr/sbin/mdadm --detail --scan",
        *LINUX_SG_SES_SUDO_COMMANDS,
        *SMARTCTL_SUDO_COMMANDS,
        *LINUX_NVME_SUDO_COMMANDS,
    ),
    "quantastor": (
        *LINUX_SG_SES_SUDO_COMMANDS,
        *SMARTCTL_SUDO_COMMANDS,
    ),
    "esxi": (),
}
SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "core": (
        *CORE_SESUTIL_LOCATE_SUDO_COMMANDS,
        *CORE_SMARTCTL_SUDO_COMMANDS,
        *CORE_MPRUTIL_SUDO_COMMANDS,
        *CORE_DMIDECODE_SUDO_COMMANDS,
        *CORE_MESSAGES_SUDO_COMMANDS,
        *CORE_MIDCLT_DISK_SYNC_SUDO_COMMANDS,
    ),
    "scale": (
        LINUX_SG_SES_SUDO_COMMANDS[3],
        LINUX_SG_SES_SUDO_COMMANDS[4],
        *SMARTCTL_SUDO_COMMANDS,
        *SCALE_MIDCLT_DISK_SYNC_SUDO_COMMANDS,
    ),
    "linux": (
        *LINUX_SG_SES_SUDO_COMMANDS,
        *SMARTCTL_SUDO_COMMANDS,
        *LINUX_NVME_SUDO_COMMANDS,
    ),
    "quantastor": (
        LINUX_SG_SES_SUDO_COMMANDS[3],
        LINUX_SG_SES_SUDO_COMMANDS[4],
        *SMARTCTL_SUDO_COMMANDS,
    ),
    "esxi": (),
}


def saved_sudo_commands_for_system(settings: Settings, source_system_id: str | None) -> list[str]:
    """Return the saved ``sudo ...`` SSH command lines of a configured system.

    The admin editor hides saved commands behind placeholders, so bootstrap and the
    sudoers preview cannot read them from the request body. They send the saved
    system's id instead and the server resolves the list here, mirroring the
    ``sudo`` prefix filter the editor applies to visible command text. A blank id
    yields an empty list (platform defaults apply); an id that names no configured
    system raises so a stale editor state fails closed instead of silently
    installing the default set.
    """
    normalized_id = normalize_text(source_system_id)
    if not normalized_id:
        return []
    for system in settings.systems:
        if system.id != normalized_id:
            continue
        return [
            str(command).strip()
            for command in system.ssh.commands
            if SAVED_SUDO_COMMAND_PREFIX.match(str(command).strip())
        ]
    raise ValueError(SAVED_SSH_COMMANDS_UNAVAILABLE_DETAIL)


class ServiceAccountBootstrapService:
    def __init__(
        self,
        config_path: str,
        *,
        probe_factory: Callable[[SSHConfig], SSHProbe] = SSHProbe,
        known_hosts_path: str | None = None,
    ) -> None:
        self.config_path = config_path
        # The operator's configured trust file (settings.ssh.known_hosts_path);
        # never taken from the request. Falls back to the runtime layout.
        self.known_hosts_path = known_hosts_path or _derive_runtime_layout_paths(config_path)["known_hosts_path"]
        self.key_manager = SSHKeyManager(config_path)
        self.probe_factory = probe_factory

    def bootstrap_service_account(self, payload: SystemSetupBootstrapRequest) -> dict[str, object]:
        if payload.platform == "esxi":
            raise ValueError(ESXI_BOOTSTRAP_UNSUPPORTED_DETAIL)
        requires_sudo = self._requires_sudo(payload.bootstrap_user)
        if requires_sudo and payload.bootstrap_sudo_password:
            # The script travels on stdin, which a piped sudo password would share.
            raise ValueError(BOOTSTRAP_SUDO_PASSWORD_UNSUPPORTED_DETAIL)
        public_key, key_source = self._resolve_public_key(payload)
        ssh_config = SSHConfig(
            enabled=True,
            host=payload.host,
            port=payload.port,
            user=payload.bootstrap_user,
            key_path=payload.bootstrap_key_path or "",
            password=payload.bootstrap_password or "",
            sudo_password="",
            known_hosts_path=self.known_hosts_path,
            strict_host_key_checking=payload.bootstrap_strict_host_key_checking,
            timeout_seconds=payload.timeout_seconds,
            commands=[],
        )
        probe = self.probe_factory(ssh_config)
        command = self._build_bootstrap_command(payload)
        script = self._build_remote_script(payload, public_key) + "\n"
        result = probe.run_command_sync(command, stdin_data=script)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown SSH bootstrap error."
            raise ValueError(detail)

        details = self._parse_output(result.stdout)
        sudo_verified = details.get("sudo_rules_installed") == "1"
        policy_unverified = payload.install_sudo_rules and not sudo_verified
        account_created = details.get("account_created") == "1"
        provisioned = (
            f"Created {payload.service_user} on {payload.host} through the TrueNAS middleware"
            if account_created
            else f"Provisioned {payload.service_user} on {payload.host}"
        )
        return {
            "ok": not policy_unverified,
            "host": payload.host,
            "platform": payload.platform,
            "bootstrap_user": payload.bootstrap_user,
            "service_user": details.get("service_user") or payload.service_user,
            "service_home": details.get("service_home"),
            "authorized_keys_path": details.get("authorized_keys_path"),
            "sudo_rules_installed": payload.install_sudo_rules and sudo_verified,
            "account_created": account_created,
            "sudoers_path": details.get("sudoers_path"),
            "permission_target": details.get("permission_target") or details.get("sudoers_path"),
            "key_source": key_source,
            "detail": (
                f"{provisioned} using one-time setup credentials. "
                "Those bootstrap credentials were used only for this action and were not written to config.yaml."
                if not policy_unverified else
                "The requested sudo policy installation could not be verified. Account setup may have completed."
            ),
            "stdout": result.stdout.strip(),
        }

    def _resolve_public_key(self, payload: SystemSetupBootstrapRequest) -> tuple[str, str]:
        if payload.service_key_name:
            for key in self.key_manager.list_keys():
                if key.get("name") == payload.service_key_name and key.get("public_key"):
                    return str(key["public_key"]).strip(), f"managed key {payload.service_key_name}"

        if payload.service_key_path:
            key_path = Path(payload.service_key_path)
            if not key_path.exists():
                raise ValueError(f"SSH key file '{payload.service_key_path}' was not found in the admin container.")
            public_path = Path(f"{key_path}.pub")
            if public_path.exists():
                return self._load_public_key_text(public_path.read_text(encoding="utf-8")), str(public_path)
            return self._derive_public_key_from_private_key(key_path.read_bytes()), str(key_path)

        if payload.service_public_key:
            return self._load_public_key_text(payload.service_public_key), "inline public key"

        raise ValueError("No usable SSH key could be resolved for bootstrap.")

    @staticmethod
    def _load_public_key_text(content: str) -> str:
        for line in str(content).splitlines():
            stripped = line.strip()
            if stripped.startswith(PUBLIC_KEY_PREFIXES):
                return stripped
        raise ValueError("The provided SSH public key text is not in a supported OpenSSH format.")

    @classmethod
    def _derive_public_key_from_private_key(cls, payload: bytes) -> str:
        for loader in (
            serialization.load_ssh_private_key,
            serialization.load_pem_private_key,
        ):
            try:
                private_key = loader(payload, password=None)
                return private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.OpenSSH,
                    format=serialization.PublicFormat.OpenSSH,
                ).decode("ascii")
            except (TypeError, ValueError):
                continue
        raise ValueError("The selected SSH key path does not contain a supported private key.")

    @staticmethod
    def _core_service_home(payload: SystemSetupBootstrapRequest) -> str | None:
        """Return the validated CORE home for a new account, or None when unset.

        The middleware only keeps SSH keys for homes under /mnt and refuses a
        home that is not on a pool. Bootstrap also refuses the pool root itself
        and anything outside a conservative character set, so the value is safe
        to hand to `user.create` (#947). Other platforms ignore the field.
        """
        if payload.platform != "core":
            return None
        home = normalize_text(payload.service_home)
        if not home:
            return None
        if home.endswith("/"):
            home = home[:-1]
        if not home.startswith("/mnt/"):
            raise ValueError(CORE_SERVICE_HOME_INVALID_DETAIL)
        segments = home[len("/mnt/"):].split("/")
        if len(segments) < 2:
            raise ValueError(CORE_SERVICE_HOME_INVALID_DETAIL)
        for segment in segments:
            if segment in {".", ".."} or not CORE_SERVICE_HOME_SEGMENT.fullmatch(segment):
                raise ValueError(CORE_SERVICE_HOME_INVALID_DETAIL)
        return home

    def _build_bootstrap_command(self, payload: SystemSetupBootstrapRequest) -> str:
        # sshd hands this string to the login user's shell, which is csh for root
        # on TrueNAS CORE. Keep it one line with no quoting; the script itself
        # goes on stdin. FreeBSD's /bin/sh has no -l option (#947).
        if self._requires_sudo(payload.bootstrap_user):
            return "sudo -n /bin/sh -s"
        return "/bin/sh -s"

    @staticmethod
    def _requires_sudo(bootstrap_user: str) -> bool:
        normalized = str(bootstrap_user or "").strip().lower()
        return normalized not in {"root", "toor"}

    def _build_remote_script(self, payload: SystemSetupBootstrapRequest, public_key: str) -> str:
        service_user = payload.service_user
        service_shell = payload.service_shell
        install_sudo = "1" if payload.install_sudo_rules else "0"
        core_midclt_payload = (
            self._build_core_midclt_user_update_payload(payload.sudo_commands)
            if payload.install_sudo_rules and payload.platform == "core"
            else ""
        )
        sudoers_content = (
            self._build_sudoers_content(service_user, payload.platform, payload.sudo_commands)
            if payload.install_sudo_rules and payload.platform != "core"
            else ""
        )
        if payload.platform == "core":
            home_error = None
            try:
                service_home = self._core_service_home(payload)
            except ValueError:
                # Existence is only known after the remote read-only lookup.
                # Never send an invalid home; reject it only if creation is needed.
                service_home = None
                home_error = CORE_SERVICE_HOME_INVALID_DETAIL
            account_lines = self._core_account_lookup_script(
                self._build_core_midclt_user_create_payload(
                    service_user, service_shell, service_home, public_key
                )
                if service_home
                else None,
                service_home,
                home_error=home_error,
            )
        else:
            account_lines = [
                "if id \"$svc_user\" >/dev/null 2>&1; then",
                "  :",
                "elif command -v pw >/dev/null 2>&1; then",
                "  pw useradd \"$svc_user\" -m -s \"$svc_shell\"",
                "elif command -v useradd >/dev/null 2>&1; then",
                "  useradd -m -s \"$svc_shell\" \"$svc_user\"",
                "else",
                "  echo \"No supported user-management command was found on the remote host.\" >&2",
                "  exit 1",
                "fi",
                "home_dir=$(awk -F: -v user=\"$svc_user\" '$1 == user { print $6 }' /etc/passwd | head -n1)",
                "if [ -z \"$home_dir\" ]; then",
                "  home_dir=\"/home/$svc_user\"",
                "fi",
            ]
        script_lines = [
            *account_lines,
            "ssh_dir=\"$home_dir/.ssh\"",
            "authorized_keys_path=\"$ssh_dir/authorized_keys\"",
            "mkdir -p \"$ssh_dir\"",
            "chmod 700 \"$ssh_dir\"",
            "touch \"$authorized_keys_path\"",
            "if ! grep -Fqx \"$pubkey\" \"$authorized_keys_path\" 2>/dev/null; then",
            "  printf '%s\\n' \"$pubkey\" >> \"$authorized_keys_path\"",
            "fi",
            "chmod 600 \"$authorized_keys_path\"",
            "primary_group=$(id -gn \"$svc_user\" 2>/dev/null || echo \"$svc_user\")",
            "chown -R \"$svc_user\":\"$primary_group\" \"$ssh_dir\" 2>/dev/null || chown -R \"$svc_user\" \"$ssh_dir\"",
            "sudoers_path=",
            "permission_target=",
            "if [ \"$install_sudo\" = \"1\" ]; then",
        ]
        if payload.platform == "core":
            script_lines.extend(
                [
                    f"  midclt_payload={shlex.quote(core_midclt_payload)}",
                    "  midclt call user.update \"$svc_id\" \"$midclt_payload\" >/dev/null",
                    "  sudoers_path=\"midclt:user.update:$svc_id\"",
                    "  permission_target=\"$sudoers_path\"",
                ]
            )
        else:
            script_lines.extend(
                [
                    *self._linux_sudo_policy_script(service_user, sudoers_content).splitlines(),
                ]
            )
        script_lines.extend(
            [
            "fi",
            "printf 'BOOTSTRAP_SERVICE_USER=%s\\n' \"$svc_user\"",
            "printf 'BOOTSTRAP_SERVICE_HOME=%s\\n' \"$home_dir\"",
            "printf 'BOOTSTRAP_AUTHORIZED_KEYS_PATH=%s\\n' \"$authorized_keys_path\"",
            "printf 'BOOTSTRAP_SUDOERS_PATH=%s\\n' \"$sudoers_path\"",
            "printf 'BOOTSTRAP_PERMISSION_TARGET=%s\\n' \"$permission_target\"",
            "printf 'BOOTSTRAP_SUDO_RULES_INSTALLED=%s\\n' \"$install_sudo\"",
            ]
        )
        if payload.platform == "core":
            script_lines.append("printf 'BOOTSTRAP_ACCOUNT_CREATED=%s\\n' \"$account_created\"")
        # `/bin/sh -s` reads this script from stdin. The shell parses the whole
        # function before running it, and the body's own stdin is /dev/null, so
        # no command it runs can read the rest of the script as its input.
        return "\n".join(
            [
                "set -eu",
                f"svc_user={shlex.quote(service_user)}",
                f"svc_shell={shlex.quote(service_shell)}",
                f"pubkey={shlex.quote(public_key)}",
                f"install_sudo={shlex.quote(install_sudo)}",
                "bootstrap_main() {",
                *script_lines,
                "}",
                "bootstrap_main </dev/null",
            ]
        )

    @staticmethod
    def _core_account_query_lines() -> list[str]:
        return [
            "svc_user_json=$(midclt call user.query \"$svc_query\" '{\"select\":[\"id\",\"username\",\"home\"]}')",
            (
                "svc_id=$(printf '%s\\n' \"$svc_user_json\" | "
                "sed -n 's/.*\"id\"[[:space:]]*:[[:space:]]*\\([0-9][0-9]*\\).*/\\1/p' | head -n1)"
            ),
            (
                "home_dir=$(printf '%s\\n' \"$svc_user_json\" | "
                "sed -n 's/.*\"home\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p' | head -n1)"
            ),
        ]

    @classmethod
    def _core_account_lookup_script(
        cls,
        create_payload: str | None = None,
        service_home: str | None = None,
        *,
        home_error: str | None = None,
    ) -> list[str]:
        """Resolve the CORE middleware account before anything on the host changes.

        TrueNAS CORE regenerates /etc/master.passwd from the middleware, so an
        account made with `pw useradd` disappears and cannot be granted sudo
        through `user.update` (#947). A missing account is created with one
        `user.create` call when the operator gave a home on a pool; otherwise
        bootstrap stops and asks for one. An existing account keeps its home.
        """
        nothing_changed = "Nothing was changed on the host."
        if home_error:
            missing_lines = [
                f"  printf '%s\\n' {shlex.quote(f'{home_error} {nothing_changed}')} >&2",
                "  exit 1",
            ]
        elif create_payload and service_home:
            missing_lines = [
                f"  create_payload={shlex.quote(create_payload)}",
                "  if ! create_output=$(midclt call user.create \"$create_payload\" 2>&1); then",
                (
                    "    echo \"TrueNAS CORE could not create the account $svc_user with home directory "
                    f"{service_home}: $create_output\" >&2"
                ),
                "    exit 1",
                "  fi",
                "  account_created=1",
                *(f"  {line}" for line in cls._core_account_query_lines()),
                "  if [ -z \"$svc_id\" ]; then",
                (
                    "    echo \"TrueNAS CORE reported creating $svc_user, but the account was not found "
                    "afterwards. Check Accounts > Users, then run this setup again.\" >&2"
                ),
                "    exit 1",
                "  fi",
            ]
        else:
            missing_lines = [
                (
                    "  echo \"The TrueNAS CORE account $svc_user does not exist. "
                    f"{nothing_changed} To create it, {CORE_SERVICE_HOME_FIELD_HELP}. Or create it "
                    f"in the CORE web UI under Accounts > Users {CORE_SERVICE_HOME_HELP}. "
                    "Then run this setup again.\" >&2"
                ),
                "  exit 1",
            ]
        return [
            "if ! command -v midclt >/dev/null 2>&1; then",
            f"  echo \"TrueNAS CORE midclt was not found on the remote host. {nothing_changed}\" >&2",
            "  exit 1",
            "fi",
            "account_created=0",
            "svc_query=$(printf '[[\"username\",\"=\",\"%s\"]]' \"$svc_user\")",
            *cls._core_account_query_lines(),
            "if [ -z \"$svc_id\" ]; then",
            *missing_lines,
            "fi",
            "pool_root=/mnt",
            "case \"$home_dir\" in",
            "  \"$pool_root\"/?*) ;;",
            "  *)",
            (
                "    echo \"The TrueNAS CORE account $svc_user has home directory '$home_dir'. "
                "CORE keeps SSH keys only for accounts whose home directory is under /mnt. "
                f"{nothing_changed} In Accounts > Users, edit $svc_user {CORE_SERVICE_HOME_HELP}, "
                "then run this setup again.\" >&2"
            ),
            "    exit 1",
            "    ;;",
            "esac",
        ]

    @classmethod
    def _linux_sudo_policy_script(cls, service_user: str, sudoers_content: str) -> str:
        # Support only direct, unquoted includedir directives in the conventional
        # Linux root policy, not arbitrary include graphs. The default visudo
        # report must confirm the root and final file, not just fragment syntax.
        return "\n".join([
            "  sudoers_dir=",
            "  export LC_ALL=C",
            "  policy_error() { echo \"Unable to verify sudo policy: $*\" >&2; exit 1; }",
            "  command -v visudo >/dev/null 2>&1 || policy_error 'visudo is required.'",
            "  command -v timeout >/dev/null 2>&1 || policy_error 'timeout is required.'",
            "  [ -f /etc/sudoers ] && [ -r /etc/sudoers ] || policy_error 'root policy is unavailable.'",
            "  policy=$(head -c 65537 /etc/sudoers && printf '.') || policy_error 'root policy read failed.'",
            "  policy=${policy%.}",
            "  [ ${#policy} -le 65536 ] || policy_error 'root policy exceeds 64 KiB.'",
            "  included_dirs=$(printf '%s\\n' \"$policy\" | awk '",
            r"    /\\$/ { bad=1 }",
            "    $1 ~ /^[@#]include/ {",
            "      if (($1 != \"#includedir\" && $1 != \"@includedir\") ||",
            "          ($2 != \"/etc/sudoers.d\" && $2 != \"/usr/local/etc/sudoers.d\") ||",
            "          (NF > 2 && $3 !~ /^#/)) bad=1; else dirs[$2]=1",
            "    }",
            "    END { if (bad) exit 1; for (dir in dirs) print dir }",
            "  ') || policy_error 'unsupported include syntax or continued policy lines.'",
            f"  for candidate in {' '.join(SUDOERS_DIR_CANDIDATES)}; do",
            "    if printf '%s\\n' \"$included_dirs\" | grep -Fxq \"$candidate\"; then",
            "      sudoers_dir=$candidate; break",
            "    fi",
            "  done",
            "  [ -n \"$sudoers_dir\" ] || policy_error 'no supported active includedir was found.'",
            "  mkdir -p \"$sudoers_dir\" || policy_error 'includedir is not writable.'",
            f'  sudoers_path="$sudoers_dir/{cls._sudoers_filename(service_user)}"',
            '  temp_sudoers_path="$sudoers_path.tmp"',
            '  policy_check=$(mktemp "$sudoers_dir/.jbod-policy.XXXXXX")',
            "  trap 'rm -f \"$policy_check\" \"$temp_sudoers_path\"' 0",
            "  check_sudo_policy() {",
            "    if ! (ulimit -f 128 && timeout 10 visudo -c > \"$policy_check\" 2>&1); then",
            "      policy_error 'default policy validation failed or exceeded its bounds.'",
            "    fi",
            "    grep -Fxq '/etc/sudoers: parsed OK' \"$policy_check\" ||",
            "      policy_error 'the default policy root is unsupported.'",
            "  }",
            "  check_sudo_policy",
            "  cat > \"$temp_sudoers_path\" <<'EOF_SUDOERS'",
            sudoers_content,
            "EOF_SUDOERS",
            '  chmod 440 "$temp_sudoers_path"',
            '  timeout 10 visudo -cf "$temp_sudoers_path" >/dev/null 2>&1 ||',
            "    policy_error 'the requested fragment failed validation.'",
            '  mv "$temp_sudoers_path" "$sudoers_path"',
            "  check_sudo_policy",
            '  grep -Fxq "$sudoers_path: parsed OK" "$policy_check" ||',
            "    policy_error 'the installed file is not confirmed in the default policy; inspect it manually.'",
            '  rm -f "$policy_check"',
            "  trap - 0",
            '  permission_target="$sudoers_path"',
        ])

    @staticmethod
    def _sudoers_filename(service_user: str) -> str:
        return f"truenas-jbod-ui-{service_user}"

    @classmethod
    def build_sudoers_preview(
        cls,
        service_user: str,
        platform: str,
        *,
        install_sudo_rules: bool = True,
        requested_commands: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        filename = cls._sudoers_filename(service_user)
        path_candidates = [f"{directory}/{filename}" for directory in SUDOERS_DIR_CANDIDATES]
        if platform == "esxi":
            return {
                "enabled": False,
                "filename": filename,
                "path_candidates": path_candidates,
                "detail": ESXI_BOOTSTRAP_UNSUPPORTED_DETAIL,
                "content": (
                    "# VMware ESXi does not use the Linux sudoers/bootstrap flow.\n"
                    "# Keep the saved SSH credentials or key directly on the system entry instead.\n"
                ),
            }
        if not install_sudo_rules:
            if platform == "core":
                return {
                    "enabled": False,
                    "filename": "midclt user.update USER_ID",
                    "path_candidates": [],
                    "detail": (
                        "Command-limited sudo is currently off, so CORE bootstrap would skip running "
                        "the middleware user.update permission command."
                    ),
                    "content": "# CORE midclt permission update disabled for this bootstrap run.\n",
                }
            return {
                "enabled": False,
                "filename": filename,
                "path_candidates": path_candidates,
                "detail": (
                    "Command-limited sudo is currently off, so bootstrap would skip writing a sudoers file "
                    "for the final service account."
                ),
                "content": "# Sudo rules disabled for this bootstrap run.\n",
            }
        if any(cls._sudo_command_contains_credentials(command) for command in requested_commands or ()):
            raise ValueError(
                "Sudoers preview cannot display commands with credential-shaped arguments."
            )
        if platform == "core":
            return {
                "enabled": True,
                "filename": "midclt user.update USER_ID",
                "path_candidates": [],
                "detail": (
                    "TrueNAS CORE bootstrap runs this same one-line middleware command after resolving "
                    f"USER_ID for {service_user} on the host. If {service_user} does not exist yet, "
                    f"{CORE_SERVICE_HOME_FIELD_HELP} first."
                ),
                "content": cls._build_core_midclt_user_update_command(
                    "USER_ID",
                    requested_commands=requested_commands,
                )
                + "\n",
            }
        return {
            "enabled": True,
            "filename": filename,
            "path_candidates": path_candidates,
            "detail": (
                "Bootstrap writes this file only to a directly included sudoers.d directory, "
                "then requires bounded visudo validation of the default policy and installed file. "
                "Unsupported include layouts fail verification."
            ),
            "content": cls._build_sudoers_content(service_user, platform, requested_commands),
        }

    @classmethod
    def _build_sudoers_content(
        cls,
        service_user: str,
        platform: str,
        requested_commands: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        if platform == "esxi":
            raise ValueError(ESXI_BOOTSTRAP_UNSUPPORTED_DETAIL)
        commands = cls._resolve_sudo_commands(platform, requested_commands)
        alias_name = f"JBODMAP_{re.sub(r'[^A-Z0-9]+', '_', platform.upper())}_CMDS"
        lines = [
            f"# Managed by truenas-jbod-ui admin bootstrap for {service_user}",
            f"Defaults:{service_user} !requiretty",
        ]
        if commands:
            wrapped_commands = ", \\\n  ".join(commands)
            lines.append(f"Cmnd_Alias {alias_name} = {wrapped_commands}")
            lines.append(f"{service_user} ALL=(root) NOPASSWD: {alias_name}")
        else:
            lines.append(f"{service_user} ALL=(root) NOPASSWD: ALL")
        return "\n".join(lines) + "\n"

    @classmethod
    def _build_core_midclt_user_update_payload(
        cls,
        requested_commands: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        return json.dumps(
            {
                "sudo": True,
                "sudo_nopasswd": True,
                "sudo_commands": list(cls._resolve_sudo_commands("core", requested_commands)),
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _build_core_midclt_user_create_payload(
        service_user: str,
        service_shell: str,
        service_home: str,
        public_key: str,
    ) -> str:
        """JSON for one `midclt call user.create` of a missing CORE account.

        The middleware makes the home with ``home_mode``, owns it and writes
        authorized_keys from ``sshpubkey``. No password is sent; the account
        logs in by key only. Grants are added afterwards by `user.update`.
        """
        return json.dumps(
            {
                "username": service_user,
                "full_name": CORE_SERVICE_ACCOUNT_FULL_NAME,
                "group_create": True,
                "home": service_home,
                "home_mode": "700",
                "shell": service_shell,
                "password_disabled": True,
                "smb": False,
                "sshpubkey": public_key,
            },
            separators=(",", ":"),
        )

    @classmethod
    def _build_core_midclt_user_update_command(
        cls,
        user_id: str,
        *,
        requested_commands: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        payload = cls._build_core_midclt_user_update_payload(requested_commands)
        return f"midclt call user.update {shlex.quote(str(user_id))} {shlex.quote(payload)}"

    @classmethod
    def _resolve_sudo_commands(
        cls,
        platform: str,
        requested_commands: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        normalized_requested = cls._normalize_requested_sudo_commands(requested_commands or [])
        if normalized_requested:
            return cls._dedupe_commands(
                (
                    *normalized_requested,
                    *SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM.get(platform, ()),
                )
            )
        return cls._dedupe_commands(SUDO_COMMANDS_BY_PLATFORM.get(platform, ()))

    @classmethod
    def _normalize_requested_sudo_commands(cls, commands: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for command in commands:
            cleaned = cls._normalize_requested_sudo_command(command)
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return tuple(normalized)

    @staticmethod
    def _normalize_requested_sudo_command(command: str) -> str | None:
        raw_command = str(command or "").strip()
        if not raw_command:
            return None
        try:
            tokens = shlex.split(raw_command)
        except ValueError:
            tokens = raw_command.split()

        if not tokens or tokens[0] != "sudo":
            return None

        remainder = tokens[1:]
        while remainder and remainder[0].startswith("-"):
            remainder.pop(0)
        if not remainder:
            return None

        executable = remainder[0]
        args = remainder[1:]
        executable_name = Path(executable).name.lower()

        if executable_name == "sg_ses":
            target_device = next(
                (arg for arg in reversed(args) if re.fullmatch(r"/dev/sg[0-9]+", arg)),
                None,
            )
            page_name = None
            for index, arg in enumerate(args):
                if arg == "-p" and index + 1 < len(args):
                    candidate = args[index + 1].lower()
                    if candidate in {"aes", "ec"}:
                        page_name = candidate
                        break
                if arg.lower() in {"aes", "ec"}:
                    page_name = arg.lower()
                    break
            if target_device and page_name:
                return f"{executable} ^-p {page_name} /dev/sg[0-9]+$"
            if target_device and "--join" in args and "--filter" in args:
                return f"{executable} ^--join --filter /dev/sg[0-9]+$"
            if target_device and any(arg == "--set=ident" for arg in args):
                return f"{executable} ^--dev-slot-num=[0-9]+ --set=ident /dev/sg[0-9]+$"
            if target_device and any(arg == "--clear=ident" for arg in args):
                return f"{executable} ^--dev-slot-num=[0-9]+ --clear=ident /dev/sg[0-9]+$"

        if executable_name == "sesutil" and len(args) >= 5 and args[:2] == ["locate", "-u"]:
            state = args[-1].lower()
            if args[2].startswith("/dev/ses") and state in {"on", "off"}:
                return f"{executable} ^locate -u /dev/ses[0-9]+ [0-9]+ {state}$"

        if executable_name == "mprutil":
            if len(args) >= 2 and args[0].lower() == "show":
                subcommand = args[1].lower()
                if subcommand in CORE_MPRUTIL_SHOW_COMMANDS:
                    return f"{executable} show {subcommand}"
            if len(args) >= 4 and args[0].lower() == "-u" and args[2].lower() == "show":
                subcommand = args[3].lower()
                if subcommand in CORE_MPRUTIL_UNIT_SHOW_COMMANDS:
                    return f"{executable} ^-u [0-9]+ show {subcommand}$"

        if executable_name == "dmidecode":
            lowered_args = [arg.lower() for arg in args]
            if ("-t" in lowered_args or "--type" in lowered_args) and any(
                token in lowered_args for token in {"slot", "9"}
            ):
                return f"{executable} -t slot"

        if executable_name == "tail":
            lowered_args = [arg.lower() for arg in args]
            if "/var/log/messages" in args and "-n" in lowered_args:
                return f"{executable} -n 4000 /var/log/messages"

        return shlex.join([executable, *args])

    @staticmethod
    def _sudo_command_contains_credentials(command: str) -> bool:
        raw_command = str(command or "")
        redacted_command = redact_ssh_command(raw_command)
        try:
            return shlex.split(redacted_command) != shlex.split(raw_command)
        except ValueError:
            return redacted_command != raw_command

    @staticmethod
    def _dedupe_commands(commands: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        deduped: list[str] = []
        for command in commands:
            cleaned = str(command or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped.append(cleaned)
        return tuple(deduped)

    @staticmethod
    def _parse_output(stdout: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for line in stdout.splitlines():
            if line.startswith("BOOTSTRAP_SERVICE_USER="):
                parsed["service_user"] = line.split("=", 1)[1].strip()
            elif line.startswith("BOOTSTRAP_SERVICE_HOME="):
                parsed["service_home"] = line.split("=", 1)[1].strip()
            elif line.startswith("BOOTSTRAP_AUTHORIZED_KEYS_PATH="):
                parsed["authorized_keys_path"] = line.split("=", 1)[1].strip()
            elif line.startswith("BOOTSTRAP_SUDOERS_PATH="):
                parsed["sudoers_path"] = line.split("=", 1)[1].strip()
            elif line.startswith("BOOTSTRAP_SUDO_RULES_INSTALLED="):
                parsed["sudo_rules_installed"] = line.split("=", 1)[1].strip()
            elif line.startswith("BOOTSTRAP_PERMISSION_TARGET="):
                parsed["permission_target"] = line.split("=", 1)[1].strip()
            elif line.startswith("BOOTSTRAP_ACCOUNT_CREATED="):
                parsed["account_created"] = line.split("=", 1)[1].strip()
        return parsed
