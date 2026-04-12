from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass

import paramiko

from app.config import SSHConfig

logger = logging.getLogger(__name__)


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

    def _run_commands_sync(self) -> list[SSHCommandResult]:
        results: list[SSHCommandResult] = []
        with self._client() as client:
            for command in self.config.commands:
                results.append(self._run_single_command(client, command))

        return results

    def _run_command_sync(self, command: str) -> SSHCommandResult:
        with self._client() as client:
            return self._run_single_command(client, command)

    def _client(self):
        client = paramiko.SSHClient()

        if self.config.strict_host_key_checking:
            if self.config.known_hosts_path:
                client.load_host_keys(self.config.known_hosts_path)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        client.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.user,
            key_filename=self.config.key_path,
            look_for_keys=False,
            allow_agent=False,
            timeout=self.config.timeout_seconds,
            banner_timeout=self.config.timeout_seconds,
            auth_timeout=self.config.timeout_seconds,
        )
        return client

    def _run_single_command(self, client: paramiko.SSHClient, command: str) -> SSHCommandResult:
        logger.debug("Running SSH command: %s", command)
        effective_command, sudo_password = self._prepare_command(command)
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
