from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path

import paramiko

from app.config import SSHConfig

logger = logging.getLogger(__name__)


class AutoPinHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Trust on first use, then persist the observed host key."""

    def __init__(self, known_hosts_path: str) -> None:
        self.known_hosts_path = known_hosts_path

    def missing_host_key(
        self,
        client: paramiko.SSHClient,
        hostname: str,
        key: paramiko.PKey,
    ) -> None:
        client._host_keys.add(hostname, key.get_name(), key)
        client.save_host_keys(self.known_hosts_path)
        logger.info(
            "Pinned new SSH host key for %s (%s) in %s",
            hostname,
            key.get_name(),
            self.known_hosts_path,
        )


@dataclass(slots=True)
class SSHCommandResult:
    command: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


class SSHProbe:
    def __init__(self, config: SSHConfig) -> None:
        self.config = config

    def open_client(self) -> paramiko.SSHClient:
        if not self.config.enabled:
            raise ValueError("SSH fallback is disabled.")
        return self._client()

    async def run_commands(self) -> list[SSHCommandResult]:
        if not self.config.enabled:
            return []
        return await asyncio.to_thread(self._run_commands_sync)

    async def run_command(self, command: str) -> SSHCommandResult:
        if not self.config.enabled:
            return SSHCommandResult(
                command=command,
                ok=False,
                stderr="SSH fallback is disabled.",
                exit_code=1,
            )
        return await asyncio.to_thread(self._run_command_sync, command)

    def run_command_sync(self, command: str) -> SSHCommandResult:
        if not self.config.enabled:
            return SSHCommandResult(
                command=command,
                ok=False,
                stderr="SSH fallback is disabled.",
                exit_code=1,
            )
        return self._run_command_sync(command)

    def _run_commands_sync(self) -> list[SSHCommandResult]:
        results: list[SSHCommandResult] = []
        try:
            with self._client() as client:
                for command in self.config.commands:
                    results.append(self._run_single_command(client, command))
        except Exception as exc:
            logger.warning(
                "SSH command batch failed for %s@%s: %s",
                self.config.user,
                self.config.host,
                exc,
            )
            error_message = str(exc) or exc.__class__.__name__
            return [self._failure_result(command, error_message) for command in self.config.commands]

        return results

    def _run_command_sync(self, command: str) -> SSHCommandResult:
        try:
            with self._client() as client:
                return self._run_single_command(client, command)
        except Exception as exc:
            logger.warning(
                "SSH command failed to start for %s@%s: %s",
                self.config.user,
                self.config.host,
                exc,
            )
            error_message = str(exc) or exc.__class__.__name__
            return self._failure_result(command, error_message)

    def _client(self):
        client = paramiko.SSHClient()

        if self.config.strict_host_key_checking:
            if self.config.known_hosts_path:
                known_hosts_path = self._prepare_known_hosts_path(self.config.known_hosts_path)
                client.load_host_keys(known_hosts_path)
                client.set_missing_host_key_policy(AutoPinHostKeyPolicy(known_hosts_path))
            else:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.user,
            "key_filename": self.config.key_path or None,
            "password": self.config.password or None,
            "look_for_keys": False,
            "allow_agent": False,
            "timeout": self.config.timeout_seconds,
            "banner_timeout": self.config.timeout_seconds,
            "auth_timeout": self.config.timeout_seconds,
        }

        try:
            client.connect(**connect_kwargs)
        except paramiko.BadAuthenticationType as exc:
            if not self._try_keyboard_interactive(client, exc):
                raise
        return client

    @staticmethod
    def _prepare_known_hosts_path(path_value: str) -> str:
        path = Path(path_value)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        return str(path)

    def _try_keyboard_interactive(
        self,
        client: paramiko.SSHClient,
        exc: paramiko.BadAuthenticationType,
    ) -> bool:
        allowed_types = getattr(exc, "allowed_types", []) or []
        if not self.config.password or "keyboard-interactive" not in allowed_types:
            return False

        transport = client.get_transport()
        if transport is None:
            return False

        def handler(title: str, instructions: str, prompts: list[tuple[str, bool]]) -> list[str]:
            responses: list[str] = []
            for prompt, _show_input in prompts:
                if "password" in prompt.lower():
                    responses.append(self.config.password)
                else:
                    responses.append("")
            return responses

        logger.debug(
            "Falling back to keyboard-interactive SSH auth for %s@%s",
            self.config.user,
            self.config.host,
        )
        transport.auth_interactive(self.config.user, handler)
        return transport.is_authenticated()

    def _run_single_command(self, client: paramiko.SSHClient, command: str) -> SSHCommandResult:
        logger.debug("Running SSH command: %s", command)
        effective_command, sudo_password = self._prepare_command(command)
        try:
            stdin, stdout, stderr = client.exec_command(effective_command, timeout=self.config.timeout_seconds)
            if sudo_password:
                stdin.write(f"{sudo_password}\n")
                stdin.flush()
                stdin.channel.shutdown_write()
            else:
                stdin.close()
            output = stdout.read().decode("utf-8", errors="replace")
            error = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            ok = exit_code == 0
            if not ok:
                logger.warning("SSH command failed: %s (exit=%s)", command, exit_code)
            return SSHCommandResult(
                command=command,
                ok=ok,
                stdout=output,
                stderr=error,
                exit_code=exit_code,
            )
        except Exception as exc:
            logger.warning(
                "SSH command execution failed for %s@%s: %s",
                self.config.user,
                self.config.host,
                exc,
            )
            error_message = str(exc) or exc.__class__.__name__
            return self._failure_result(command, error_message)

    @staticmethod
    def _failure_result(command: str, error_message: str) -> SSHCommandResult:
        return SSHCommandResult(
            command=command,
            ok=False,
            stderr=error_message,
            exit_code=255,
        )

    def _prepare_command(self, command: str) -> tuple[str, str | None]:
        sudo_password = self.config.sudo_password or None
        if not sudo_password:
            return command, None

        try:
            tokens = shlex.split(command)
        except ValueError:
            return command, None

        if not tokens or tokens[0] != "sudo":
            return command, None

        remainder = tokens[1:]
        while remainder and remainder[0].startswith("-"):
            remainder.pop(0)

        if not remainder:
            return command, None

        effective = shlex.join(["sudo", "-S", "-p", "", *remainder])
        return effective, sudo_password
