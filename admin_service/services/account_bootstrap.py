from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization

from app.config import SSHConfig
from app.models.domain import SystemSetupBootstrapRequest
from app.services.ssh_key_manager import SSHKeyManager
from app.services.ssh_probe import SSHProbe


PUBLIC_KEY_PREFIXES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-", "sk-ssh-")
SUDOERS_DIR_CANDIDATES = ("/usr/local/etc/sudoers.d", "/etc/sudoers.d")
SUDO_COMMANDS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "core": (
        "/usr/sbin/sesutil map",
        "/usr/sbin/sesutil show",
        "/usr/sbin/sesutil locate -u /dev/ses* * on",
        "/usr/sbin/sesutil locate -u /dev/ses* * off",
        "/sbin/camcontrol devlist -v",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
    ),
    "scale": (
        "/usr/bin/sg_ses -p aes /dev/sg*",
        "/usr/bin/sg_ses -p ec /dev/sg*",
        "/usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*",
        "/usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*",
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
    ),
    "linux": (
        "/usr/sbin/mdadm --detail --scan",
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
    ),
    "quantastor": (
        "/usr/bin/sg_ses -p aes /dev/sg*",
        "/usr/bin/sg_ses -p ec /dev/sg*",
        "/usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*",
        "/usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*",
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
        "/usr/bin/qs *",
    ),
}
SUPPLEMENTAL_SUDO_COMMANDS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "core": (
        "/usr/sbin/sesutil locate -u /dev/ses* * on",
        "/usr/sbin/sesutil locate -u /dev/ses* * off",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
    ),
    "scale": (
        "/usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*",
        "/usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*",
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
    ),
    "linux": (
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
    ),
    "quantastor": (
        "/usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*",
        "/usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*",
        "/usr/sbin/smartctl -x -j *",
        "/usr/sbin/smartctl -x *",
        "/usr/local/sbin/smartctl -x -j *",
        "/usr/local/sbin/smartctl -x *",
        "/usr/bin/qs *",
    ),
}


class ServiceAccountBootstrapService:
    def __init__(
        self,
        config_path: str,
        *,
        probe_factory: Callable[[SSHConfig], SSHProbe] = SSHProbe,
    ) -> None:
        self.config_path = config_path
        self.key_manager = SSHKeyManager(config_path)
        self.probe_factory = probe_factory

    def bootstrap_service_account(self, payload: SystemSetupBootstrapRequest) -> dict[str, object]:
        public_key, key_source = self._resolve_public_key(payload)
        ssh_config = SSHConfig(
            enabled=True,
            host=payload.host,
            port=payload.port,
            user=payload.bootstrap_user,
            key_path=payload.bootstrap_key_path or "",
            password=payload.bootstrap_password or "",
            sudo_password=payload.bootstrap_sudo_password or "",
            known_hosts_path=payload.bootstrap_known_hosts_path,
            strict_host_key_checking=payload.bootstrap_strict_host_key_checking,
            timeout_seconds=payload.timeout_seconds,
            commands=[],
        )
        probe = self.probe_factory(ssh_config)
        command = self._build_bootstrap_command(payload, public_key)
        result = probe.run_command_sync(command)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown SSH bootstrap error."
            raise ValueError(detail)

        details = self._parse_output(result.stdout)
        return {
            "ok": True,
            "host": payload.host,
            "platform": payload.platform,
            "bootstrap_user": payload.bootstrap_user,
            "service_user": details.get("service_user") or payload.service_user,
            "service_home": details.get("service_home"),
            "authorized_keys_path": details.get("authorized_keys_path"),
            "sudo_rules_installed": bool(payload.install_sudo_rules),
            "sudoers_path": details.get("sudoers_path"),
            "key_source": key_source,
            "detail": (
                f"Provisioned {payload.service_user} on {payload.host} using one-time setup credentials. "
                "Those bootstrap credentials were used only for this action and were not written to config.yaml."
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
                raise ValueError(f"SSH key path '{payload.service_key_path}' does not exist inside the admin sidecar.")
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

    def _build_bootstrap_command(self, payload: SystemSetupBootstrapRequest, public_key: str) -> str:
        script = self._build_remote_script(payload, public_key)
        wrapped_script = shlex.join(["/bin/sh", "-lc", script])
        if self._requires_sudo(payload.bootstrap_user):
            return shlex.join(["sudo", "-n", "/bin/sh", "-lc", script])
        return wrapped_script

    @staticmethod
    def _requires_sudo(bootstrap_user: str) -> bool:
        normalized = str(bootstrap_user or "").strip().lower()
        return normalized not in {"root", "toor"}

    def _build_remote_script(self, payload: SystemSetupBootstrapRequest, public_key: str) -> str:
        service_user = payload.service_user
        service_shell = payload.service_shell
        install_sudo = "1" if payload.install_sudo_rules else "0"
        sudoers_content = (
            self._build_sudoers_content(service_user, payload.platform, payload.sudo_commands)
            if payload.install_sudo_rules
            else ""
        )
        script_lines = [
            "set -eu",
            f"svc_user={shlex.quote(service_user)}",
            f"svc_shell={shlex.quote(service_shell)}",
            f"pubkey={shlex.quote(public_key)}",
            f"install_sudo={shlex.quote(install_sudo)}",
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
            "if [ \"$install_sudo\" = \"1\" ]; then",
            "  sudoers_dir=",
            f"  for candidate in {' '.join(SUDOERS_DIR_CANDIDATES)}; do",
            "    if [ -d \"$candidate\" ] || mkdir -p \"$candidate\" 2>/dev/null; then",
            "      sudoers_dir=\"$candidate\"",
            "      break",
            "    fi",
            "  done",
            "  if [ -z \"$sudoers_dir\" ]; then",
            "    echo \"Unable to locate a sudoers.d directory on the remote host.\" >&2",
            "    exit 1",
            "  fi",
            f"  sudoers_path=\"$sudoers_dir/{self._sudoers_filename(service_user)}\"",
            "  temp_sudoers_path=\"$sudoers_path.tmp\"",
            "  cat > \"$temp_sudoers_path\" <<'EOF_SUDOERS'",
            sudoers_content,
            "EOF_SUDOERS",
            "  chmod 440 \"$temp_sudoers_path\"",
            "  if command -v visudo >/dev/null 2>&1; then",
            "    visudo -cf \"$temp_sudoers_path\" >/dev/null",
            "  fi",
            "  mv \"$temp_sudoers_path\" \"$sudoers_path\"",
            "fi",
            "printf 'BOOTSTRAP_SERVICE_USER=%s\\n' \"$svc_user\"",
            "printf 'BOOTSTRAP_SERVICE_HOME=%s\\n' \"$home_dir\"",
            "printf 'BOOTSTRAP_AUTHORIZED_KEYS_PATH=%s\\n' \"$authorized_keys_path\"",
            "printf 'BOOTSTRAP_SUDOERS_PATH=%s\\n' \"$sudoers_path\"",
        ]
        return "\n".join(script_lines)

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
        if not install_sudo_rules:
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
        return {
            "enabled": True,
            "filename": filename,
            "path_candidates": path_candidates,
            "detail": (
                "Bootstrap writes this exact file content after choosing the first writable sudoers.d "
                "directory on the remote host."
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
            target_device = next((arg for arg in reversed(args) if arg.startswith("/dev/sg")), None)
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
                return f"{executable} -p {page_name} /dev/sg*"
            if target_device and any(arg == "--set=ident" for arg in args):
                return f"{executable} --dev-slot-num=* --set=ident /dev/sg*"
            if target_device and any(arg == "--clear=ident" for arg in args):
                return f"{executable} --dev-slot-num=* --clear=ident /dev/sg*"

        if executable_name == "sesutil" and len(args) >= 5 and args[:2] == ["locate", "-u"]:
            state = args[-1].lower()
            if args[2].startswith("/dev/ses") and state in {"on", "off"}:
                return f"{executable} locate -u /dev/ses* * {state}"

        return shlex.join([executable, *args])

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
        return parsed
