from __future__ import annotations

import asyncio
import logging
import re
import shlex
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import paramiko

from app.config import SSHConfig

logger = logging.getLogger(__name__)

# Bound each command's combined stdout/stderr before it can enter synchronous
# parser paths. Four MiB still covers the largest supported SES page.
MAX_SSH_OUTPUT_BYTES = 4 * 1024 * 1024
# Channels one planned session runs at once over its single connection. OpenSSH
# allows ten sessions per connection by default (sshd MaxSessions); staying
# below that leaves room for the operator's own shells on the same login. A
# server with a lower MaxSessions refuses the extra channel opens; the run then
# settles on the channel count that opened (see _ChannelGate).
MAX_PARALLEL_CHANNELS_PER_CONNECTION = 8


class _ChannelGate:
    """Bound the channels open at once on one connection, lowering it on refusal."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self.active = 0
        self._condition = threading.Condition()

    def acquire(self) -> None:
        with self._condition:
            while self.active >= self.limit:
                self._condition.wait()
            self.active += 1

    def release(self) -> None:
        with self._condition:
            self.active -= 1
            self._condition.notify_all()

    def refused(self) -> bool:
        """Give back a slot whose channel open the server refused.

        Returns True when other channels are open, so the refusal is a
        per-connection session limit: the limit drops to the channels that did
        open and the caller retries once one closes. Returns False when this
        was the only channel, so retrying cannot help.
        """
        with self._condition:
            self.active -= 1
            others = self.active
            if others >= 1:
                self.limit = min(self.limit, others)
            self._condition.notify_all()
            return others >= 1

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
_KNOWN_HOSTS_LOCKS: dict[str, threading.Lock] = {}
_KNOWN_HOSTS_LOCKS_GUARD = threading.Lock()


def _known_hosts_lock(path_value: str) -> threading.Lock:
    normalized_path = str(Path(path_value).resolve())
    with _KNOWN_HOSTS_LOCKS_GUARD:
        lock = _KNOWN_HOSTS_LOCKS.get(normalized_path)
        if lock is None:
            lock = threading.Lock()
            _KNOWN_HOSTS_LOCKS[normalized_path] = lock
        return lock


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
        with _known_hosts_lock(self.known_hosts_path):
            # Each concurrent client loaded the file before connecting. Reload
            # inside the path lock so saving this new key cannot overwrite a key
            # pinned by another host session in the meantime.
            client.load_host_keys(self.known_hosts_path)
            client.get_host_keys().add(hostname, key.get_name(), key)
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

    async def run_commands(
        self,
        commands: Iterable[str] | None = None,
        *,
        stdin_data: str | None = None,
    ) -> list[SSHCommandResult]:
        if not self.config.enabled:
            if commands is None:
                return []
            return [self._failure_result(command, "SSH fallback is disabled.") for command in commands]
        command_list = self._command_list(commands)
        if not command_list:
            return []
        return await asyncio.to_thread(self._run_commands_sync, command_list, stdin_data)

    async def run_planned_commands(
        self,
        planner: CommandPlanner,
        *,
        initial_commands: Iterable[str] | None = None,
    ) -> list[SSHCommandResult]:
        if not self.config.enabled:
            return []
        return await asyncio.to_thread(self._run_planned_commands_sync, planner, initial_commands)

    async def run_planned_command_groups(
        self,
        groups: list[tuple[CommandPlanner, list[str]]],
        *,
        max_parallel_channels: int = MAX_PARALLEL_CHANNELS_PER_CONNECTION,
    ) -> list[list[SSHCommandResult]]:
        """Run several independent command plans over one connection.

        Each group behaves exactly like :meth:`run_planned_commands` would on its
        own, but all groups share one handshake and host-key check, and up to
        ``max_parallel_channels`` of them run their commands at the same time on
        separate channels. Results come back in group order.
        """
        if not self.config.enabled:
            return [[] for _ in groups]
        return await asyncio.to_thread(self._run_planned_command_groups_sync, groups, max_parallel_channels)

    def open_session(self) -> SSHSession:
        """One reusable connection for a series of commands; see :class:`SSHSession`."""
        return SSHSession(self)

    async def run_command(
        self,
        command: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SSHCommandResult:
        if not self.config.enabled:
            return SSHCommandResult(
                command=command,
                ok=False,
                stderr="SSH fallback is disabled.",
                exit_code=1,
            )
        return await asyncio.to_thread(
            self._run_command_sync,
            command,
            timeout_seconds=timeout_seconds,
        )

    def run_command_sync(
        self,
        command: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SSHCommandResult:
        if not self.config.enabled:
            return SSHCommandResult(
                command=command,
                ok=False,
                stderr="SSH fallback is disabled.",
                exit_code=1,
            )
        return self._run_command_sync(command, timeout_seconds=timeout_seconds)

    def _run_commands_sync(
        self,
        commands: Iterable[str] | None = None,
        stdin_data: str | None = None,
    ) -> list[SSHCommandResult]:
        command_list = self._command_list(commands)
        if not command_list:
            return []

        results: list[SSHCommandResult] = []
        started = time.perf_counter()
        try:
            with self._client() as client:
                for command in command_list:
                    results.append(self._run_single_command(client, command, stdin_data=stdin_data))
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
        return self._run_planned_command_groups_sync([(planner, self._command_list(initial_commands))], 1)[0]

    def _run_planned_command_groups_sync(
        self,
        groups: list[tuple[CommandPlanner, list[str]]],
        max_parallel_channels: int = MAX_PARALLEL_CHANNELS_PER_CONNECTION,
    ) -> list[list[SSHCommandResult]]:
        outcomes: list[list[SSHCommandResult]] = [[] for _ in groups]
        pending: list[tuple[int, list[str], set[str]]] = []
        for index, (planner, initial_commands) in enumerate(groups):
            seen_commands: set[str] = set()
            first_commands = self._new_commands(self._command_list(initial_commands), seen_commands)
            if not first_commands:
                first_commands = self._new_commands(planner([]), seen_commands)
            if first_commands:
                pending.append((index, first_commands, seen_commands))
        if not pending:
            return outcomes

        started = time.perf_counter()
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
            for index, first_commands, _seen in pending:
                outcomes[index] = [self._failure_result(command, error_message) for command in first_commands]
            return outcomes

        batch_counts: list[int] = [0] * len(groups)
        width = max(1, min(int(max_parallel_channels), len(pending)))
        gate = _ChannelGate(width)

        def run_one(command: str) -> SSHCommandResult:
            while True:
                gate.acquire()
                try:
                    result = self._run_single_command(client, command, raise_channel_refusal=True)
                except paramiko.ChannelException as exc:
                    if gate.refused():
                        logger.info(
                            "SSH server %s refused a session channel; running at most %s at once",
                            self.config.host,
                            gate.limit,
                        )
                        continue
                    return self._failure_result(command, str(exc) or exc.__class__.__name__)
                except BaseException:
                    gate.release()
                    raise
                gate.release()
                return result

        def drive(index: int, first_commands: list[str], seen_commands: set[str]) -> None:
            planner = groups[index][0]
            results = outcomes[index]
            pending_commands = first_commands
            session_error: Exception | None = None
            failed_pending_commands: list[str] = []
            try:
                while pending_commands:
                    batch_counts[index] += 1
                    for position, command in enumerate(pending_commands):
                        try:
                            results.append(run_one(command))
                        except Exception as exc:  # noqa: BLE001 - preserve partial batch results.
                            session_error = exc
                            failed_pending_commands = pending_commands[position:]
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

        with client:
            if width == 1:
                for item in pending:
                    drive(*item)
            else:
                with ThreadPoolExecutor(max_workers=width, thread_name_prefix="ssh-channel") as executor:
                    for future in [executor.submit(drive, *item) for item in pending]:
                        future.result()

        results_count = sum(len(outcomes[index]) for index, _commands, _seen in pending)
        logger.info(
            "SSH planned command session completed for %s@%s: connections=1 plans=%s batches=%s commands=%s failures=%s duration=%.3fs",
            self.config.user,
            self.config.host,
            len(pending),
            sum(batch_counts),
            results_count,
            sum(1 for index, _commands, _seen in pending for result in outcomes[index] if not result.ok),
            time.perf_counter() - started,
        )
        return outcomes

    def _run_command_sync(
        self,
        command: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SSHCommandResult:
        try:
            with self._client() as client:
                return self._run_single_command(
                    client,
                    command,
                    timeout_seconds=timeout_seconds,
                )
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
                known_hosts_path = Path(self.config.known_hosts_path)
                if known_hosts_path.is_file():
                    client.load_host_keys(str(known_hosts_path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            if self.config.known_hosts_path:
                known_hosts_path = self._prepare_known_hosts_path(self.config.known_hosts_path)
                client.load_host_keys(known_hosts_path)
                client.set_missing_host_key_policy(AutoPinHostKeyPolicy(known_hosts_path))
            else:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())

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
            try:
                client.connect(**connect_kwargs)
            except paramiko.BadAuthenticationType as exc:
                if not self._try_keyboard_interactive(client, exc):
                    raise
        except Exception:
            # A failed connect can leave a live transport thread and socket
            # behind; close before propagating so repeated failures can't
            # accumulate threads/FDs.
            client.close()
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

    def _run_single_command(
        self,
        client: paramiko.SSHClient,
        command: str,
        *,
        stdin_data: str | None = None,
        timeout_seconds: float | None = None,
        raise_channel_refusal: bool = False,
    ) -> SSHCommandResult:
        safe_command = redact_ssh_command(command)
        logger.debug("Running SSH command: %s", safe_command)
        effective_command, sudo_password = self._prepare_command(command)
        if stdin_data is not None and sudo_password:
            return self._failure_result(
                command,
                "SSH command input cannot be combined with sudo password input.",
            )
        try:
            command_timeout = self.config.timeout_seconds if timeout_seconds is None else timeout_seconds
            stdin, stdout, stderr = client.exec_command(effective_command, timeout=command_timeout)
            command_input = f"{sudo_password}\n" if sudo_password else stdin_data
            if command_input is not None:
                stdin.write(command_input)
                stdin.flush()
                stdin.channel.shutdown_write()
            else:
                stdin.close()
            output_bytes = stdout.read(MAX_SSH_OUTPUT_BYTES + 1)
            if len(output_bytes) > MAX_SSH_OUTPUT_BYTES:
                return self._failure_result(
                    command,
                    f"SSH command output exceeded the {MAX_SSH_OUTPUT_BYTES}-byte limit.",
                )
            remaining_bytes = MAX_SSH_OUTPUT_BYTES - len(output_bytes)
            error_bytes = stderr.read(remaining_bytes + 1)
            if len(error_bytes) > remaining_bytes:
                return self._failure_result(
                    command,
                    f"SSH command output exceeded the {MAX_SSH_OUTPUT_BYTES}-byte limit.",
                )
            output = output_bytes.decode("utf-8", errors="replace")
            error = error_bytes.decode("utf-8", errors="replace")
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
            if raise_channel_refusal and isinstance(exc, paramiko.ChannelException):
                raise
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


class SSHSession:
    """Run a series of commands over one connection, opened on first use.

    The connection comes from :meth:`SSHProbe._client`, so it is checked against
    the same host-key policy as every other connection. A connection that has
    dropped is reopened, and checked again, on the next command. Call
    :meth:`close` from a worker thread when done.
    """

    def __init__(self, probe: SSHProbe) -> None:
        self._probe = probe
        self._client: paramiko.SSHClient | None = None
        self._lock = threading.Lock()
        self.connections = 0

    def run_command(self, command: str, *, timeout_seconds: float | None = None) -> SSHCommandResult:
        with self._lock:
            try:
                client = self._connected_client()
            except Exception as exc:
                logger.warning(
                    "SSH command failed to start for %s@%s: %s",
                    self._probe.config.user,
                    self._probe.config.host,
                    exc,
                )
                return self._probe._failure_result(command, str(exc) or exc.__class__.__name__)
            return self._probe._run_single_command(client, command, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def _connected_client(self) -> paramiko.SSHClient:
        client = self._client
        transport = client.get_transport() if client is not None else None
        if client is not None and transport is not None and transport.is_active():
            return client
        if client is not None:
            client.close()
            self._client = None
        client = self._probe._client()
        self._client = client
        self.connections += 1
        return client

