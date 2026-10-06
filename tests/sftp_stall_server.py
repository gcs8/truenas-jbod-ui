"""A loopback SFTP server that stalls on purpose, for timeout regressions (#723).

It listens on 127.0.0.1 only and accepts one synthetic password login. It
stalls in one of two places: before answering the ``sftp`` subsystem request,
or after the SFTP handshake on the first ``lstat``. Every hold is bounded, so a
client without a timeout fails the test slowly instead of hanging the suite.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

import paramiko
from paramiko.common import AUTH_FAILED, AUTH_SUCCESSFUL, OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED, OPEN_SUCCEEDED
from paramiko.sftp import SFTP_NO_SUCH_FILE

USERNAME = "backup"
PASSWORD = "synthetic-sftp-password"
MAX_HOLD_SECONDS = 8.0


class _Server(paramiko.ServerInterface):
    def __init__(self, *, stall_subsystem: bool, release: threading.Event, reached: threading.Event) -> None:
        self._stall_subsystem = stall_subsystem
        self._release = release
        self._reached = reached

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        if (username, password) == (USERNAME, PASSWORD):
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return OPEN_SUCCEEDED
        return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel: paramiko.Channel, name: str) -> bool:
        if self._stall_subsystem:
            # Never answer in time: hold the request, then refuse it.
            self._reached.set()
            self._release.wait(MAX_HOLD_SECONDS)
            return False
        return super().check_channel_subsystem_request(channel, name)


class _StallingSftp(paramiko.SFTPServerInterface):
    def __init__(
        self,
        server: Any,
        *args: Any,
        release: threading.Event,
        reached: threading.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(server, *args, **kwargs)
        self._release = release
        self._reached = reached

    def lstat(self, path: str) -> int:
        self._reached.set()
        self._release.wait(MAX_HOLD_SECONDS)
        return SFTP_NO_SUCH_FILE


class StallingSftpServer:
    """Serve connections until closed; use as a context manager.

    ``reached`` is set once a client got past authentication to the stall.
    """

    def __init__(self, *, stall: str, host_key: paramiko.PKey | None = None) -> None:
        if stall not in {"subsystem", "request"}:
            raise ValueError("stall must be 'subsystem' or 'request'")
        self.stall = stall
        self.host_key = host_key or paramiko.ECDSAKey.generate()
        self.release = threading.Event()
        self.reached = threading.Event()
        self.connections = 0
        self._stopped = threading.Event()
        self._transports: list[paramiko.Transport] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="sftp-stall-server", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            transport = paramiko.Transport(connection)
            self._transports.append(transport)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler(
                "sftp", paramiko.SFTPServer, _StallingSftp, release=self.release, reached=self.reached
            )
            try:
                transport.start_server(
                    server=_Server(
                        stall_subsystem=self.stall == "subsystem", release=self.release, reached=self.reached
                    )
                )
            except (paramiko.SSHException, EOFError, OSError):
                transport.close()

    def write_known_hosts(self, path: Path) -> None:
        keys = paramiko.HostKeys()
        keys.add(f"[127.0.0.1]:{self.port}", self.host_key.get_name(), self.host_key)
        keys.save(str(path))

    def close(self) -> None:
        self._stopped.set()
        self.release.set()
        for transport in self._transports:
            transport.close()
        self._listener.close()
        self._thread.join(5)

    def __enter__(self) -> StallingSftpServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def live_client_transports() -> set[paramiko.Transport]:
    """Client-side paramiko transports whose threads are still running."""

    return {
        thread
        for thread in threading.enumerate()
        if isinstance(thread, paramiko.Transport) and not thread.server_mode and thread.is_alive()
    }
