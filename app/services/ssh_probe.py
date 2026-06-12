from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import paramiko

from app.config import SSHConfig

logger = logging.getLogger(__name__)

SENSITIVE_OPTION_NAMES = {
    "--api-key",
    "--apikey",
    "--pass",
    "--password",
    "--secret",
    "--server",
    "--token",
}
SENSITIVE_KEY_NAMES = {
    "api_key",
    "apikey",
    "pass",
    "passwd",
    "password",
    "secret",
    "token",
}
INLINE_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>api[_-]?key|pass(?:wd|word)?|secret|token)=(?P<value>[^\s'\";]+)"
)
SERVER_SPEC_RE = re.compile(r"(?i)(?P<prefix>--server=)(?P<value>[^\s'\"]+)")


def redact_ssh_command(command: str) -> str:
    """Mask inline credentials while leaving command shape useful for logs."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return _redact_unparsed_command(command)

    redacted: list[str] = []
    redact_next_option: str | None = None
    for token in tokens:
        if redact_next_option:
            redacted.append(_redact_server_spec(token) if redact_next_option == "--server" else "***")
            redact_next_option = None
            continue

        option_name, separator, option_value = token.partition("=")
        lowered_name = option_name.lower()
        if lowered_name == "--server" and not separator:
            redacted.append(token)
            redact_next_option = lowered_name
            continue
        if lowered_name == "--server" and separator:
            redacted.append(f"{option_name}={_redact_server_spec(option_value)}")
            continue
        if lowered_name in SENSITIVE_OPTION_NAMES and separator:
            redacted.append(f"{option_name}=***")
            continue
        if lowered_name in SENSITIVE_OPTION_NAMES:
            redacted.append(token)
            redact_next_option = lowered_name
            continue

        key, key_separator, value = token.partition("=")
        if key_separator and key.lower().replace("-", "_") in SENSITIVE_KEY_NAMES:
            redacted.append(f"{key}=***")
        else:
            redacted.append(token)

    return shlex.join(redacted)


def _redact_server_spec(value: str) -> str:
    parts = value.split(",", 2)
    if len(parts) >= 3:
        return f"{parts[0]},{parts[1]},***"
    return "***"


def _redact_unparsed_command(command: str) -> str:
    redacted = SERVER_SPEC_RE.sub(
        lambda match: f"{match.group('prefix')}{_redact_server_spec(match.group('value'))}",
        command,
    )
    return INLINE_SECRET_RE.sub(lambda match: f"{match.group('key')}=***", redacted)


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


CommandPlanner = Callable[[list[SSHCommandResult]], Iterable[str]]


class SSHProbe:
    def __init__(self, config: SSHConfig) -> None:
        self.config = config

    def open_client(self) -> paramiko.SSHClient:
        if not self.config.enabled:
            raise ValueError("SSH fallback is disabled.")
        return self._client()

    async def run_commands(self, commands: Iterable[str] | None = None) -> list[SSHCommandResult]:
        if not self.config.enabled:
            if commands is None:
                return []
            return [self._failure_result(command, "SSH fallback is disabled.") for command in commands]
        command_list = self._command_list(commands)
        if not command_list:
            return []
        return await asyncio.to_thread(self._run_commands_sync, command_list)

    async def run_planned_commands(
        self,
        planner: CommandPlanner,
        *,
        initial_commands: Iterable[str] | None = None,
    ) -> list[SSHCommandResult]:
        if not self.config.enabled:
            return []
        return await asyncio.to_thread(self._run_planned_commands_sync, planner, initial_commands)

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

    def _run_commands_sync(self, commands: Iterable[str] | None = None) -> list[SSHCommandResult]:
        command_list = self._command_list(commands)
        if not command_list:
            return []

        results: list[SSHCommandResult] = []
        started = time.perf_counter()
        try:
            with self._client() as client:
                for command in command_list:
                    results.append(self._run_single_command(client, command))
        except Exception as exc:
            logger.warning(
                "SSH command batch failed for %s@%s: %s",
                self.config.user,
                self.config.host,
                exc,
            )
            error_message = str(exc) or exc.__class__.__name__
            results.extend(
                self._failure_result(command, error_message)
                for command in command_list[len(results) :]
            )
            return results

        logger.info(
            "SSH command batch completed for %s@%s: connections=1 commands=%s failures=%s duration=%.3fs",
            self.config.user,
            self.config.host,
            len(results),
            sum(1 for result in results if not result.ok),
            time.perf_counter() - started,
        )
        return results

    def _run_planned_commands_sync(
        self,
        planner: CommandPlanner,
        initial_commands: Iterable[str] | None = None,
    ) -> list[SSHCommandResult]:
        results: list[SSHCommandResult] = []
        seen_commands: set[str] = set()
        pending_commands = self._new_commands(self._command_list(initial_commands), seen_commands)
        if not pending_commands:
            pending_commands = self._new_commands(planner(results), seen_commands)
        if not pending_commands:
            return []

        started = time.perf_counter()
        batch_count = 0
        try:
            client = self._client()
        except Exception as exc:
            logger.warning(
                "SSH planned command session failed for %s@%s: %s",
                self.config.user,
                self.config.host,
                exc,
            )
            error_message = str(exc) or exc.__class__.__name__
            results.extend(self._failure_result(command, error_message) for command in pending_commands)
            return results

        session_error: Exception | None = None
        failed_pending_commands: list[str] = []
        try:
            with client:
                while pending_commands:
                    batch_count += 1
                    for index, command in enumerate(pending_commands):
                        try:
                            results.append(self._run_single_command(client, command))
                        except Exception as exc:  # noqa: BLE001 - preserve partial batch results.
                            session_error = exc
                            failed_pending_commands = pending_commands[index:]
                            pending_commands = []
                            break
                    else:
                        pending_commands = self._new_commands(planner(list(results)), seen_commands)
                        continue
                    break
        except Exception as exc:  # noqa: BLE001 - preserve partial session results.
            if session_error is None:
                session_error = exc

        if session_error is not None:
            logger.warning(
                "SSH planned command session interrupted for %s@%s: %s",
                self.config.user,
                self.config.host,
                session_error,
            )
            error_message = str(session_error) or session_error.__class__.__name__
            results.extend(self._failure_result(command, error_message) for command in failed_pending_commands)
            logger.info(
                "SSH planned command session completed for %s@%s: connections=1 batches=%s commands=%s failures=%s duration=%.3fs",
                self.config.user,
                self.config.host,
                batch_count,
                len(results),
                sum(1 for result in results if not result.ok),
                time.perf_counter() - started,
            )
            return results

        logger.info(
            "SSH planned command session completed for %s@%s: connections=1 batches=%s commands=%s failures=%s duration=%.3fs",
            self.config.user,
            self.config.host,
            batch_count,
            len(results),
            sum(1 for result in results if not result.ok),
            time.perf_counter() - started,
        )
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
        safe_command = redact_ssh_command(command)
        logger.debug("Running SSH command: %s", safe_command)
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
                logger.warning("SSH command failed: %s (exit=%s)", safe_command, exit_code)
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

    def _command_list(self, commands: Iterable[str] | None = None) -> list[str]:
        return list(self.config.commands if commands is None else commands)

    @staticmethod
    def _new_commands(commands: Iterable[str], seen_commands: set[str]) -> list[str]:
        selected: list[str] = []
        for command in commands:
            if command in seen_commands:
                continue
            seen_commands.add(command)
            selected.append(command)
        return selected

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
