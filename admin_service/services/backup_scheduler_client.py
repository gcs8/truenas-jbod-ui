"""Admin-side client for the backup scheduler's Unix-socket API.

The admin sidecar authenticates and origin-checks the public
``/api/admin/backups/*`` routes, then forwards them here. The socket lives in a
directory only the scheduler and admin sidecars mount
(``BACKUP_SCHEDULER_SOCKET``, default ``/app/backup-api/scheduler.sock``).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_SOCKET = "/app/backup-api/scheduler.sock"
DEFAULT_TIMEOUT_SECONDS = 30.0
DOWNLOAD_TIMEOUT_SECONDS = 600.0
MAX_JSON_BYTES = 16 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


class SchedulerUnavailableError(RuntimeError):
    """The scheduler socket is missing or not answering."""


@dataclass
class SchedulerResponse:
    status: int
    payload: Any


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("backup-scheduler", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class BackupSchedulerClient:
    def __init__(self, socket_path: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.socket_path = socket_path or (os.getenv("BACKUP_SCHEDULER_SOCKET") or "").strip() or DEFAULT_SOCKET
        self.timeout = timeout

    def available(self) -> bool:
        try:
            return Path(self.socket_path).is_socket()
        except OSError:  # an unreadable parent means "not deployed", not a server error
            return False

    def _connection(self, timeout: float | None = None) -> _UnixConnection:
        if not self.available():
            raise SchedulerUnavailableError("The backup scheduler service is not running.")
        return _UnixConnection(self.socket_path, timeout or self.timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> SchedulerResponse:
        connection = self._connection()
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if actor:
            headers["X-Backup-Actor"] = actor
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_JSON_BYTES + 1)
        except OSError as exc:
            raise SchedulerUnavailableError("The backup scheduler service did not answer.") from exc
        finally:
            connection.close()
        if len(raw) > MAX_JSON_BYTES:
            raise SchedulerUnavailableError("The backup scheduler answer was too large.")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise SchedulerUnavailableError("The backup scheduler returned an unreadable answer.") from exc
        return SchedulerResponse(status=response.status, payload=payload)

    def download_to(self, artifact_id: str, destination: BinaryIO) -> SchedulerResponse:
        """Copy an archive into ``destination``; on HTTP errors return the JSON detail instead."""

        connection = self._connection(DOWNLOAD_TIMEOUT_SECONDS)
        try:
            connection.request("GET", f"/internal/backups/{artifact_id}/download")
            response = connection.getresponse()
            if response.status != 200:
                raw = response.read(MAX_JSON_BYTES)
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {"detail": "The backup could not be read."}
                return SchedulerResponse(status=response.status, payload=payload)
            while chunk := response.read(CHUNK_SIZE):
                destination.write(chunk)
            return SchedulerResponse(
                status=200,
                payload={
                    "filename": _filename(response.getheader("Content-Disposition")),
                    "sha256": response.getheader("X-Backup-Sha256"),
                },
            )
        except OSError as exc:
            raise SchedulerUnavailableError("The backup scheduler service did not answer.") from exc
        finally:
            connection.close()

    def stream(self, artifact_id: str) -> tuple[SchedulerResponse, Iterator[bytes] | None]:
        """Open a streaming download; the iterator closes the connection when exhausted."""

        connection = self._connection(DOWNLOAD_TIMEOUT_SECONDS)
        try:
            connection.request("GET", f"/internal/backups/{artifact_id}/download")
            response = connection.getresponse()
        except OSError as exc:
            connection.close()
            raise SchedulerUnavailableError("The backup scheduler service did not answer.") from exc
        if response.status != 200:
            try:
                payload = json.loads(response.read(MAX_JSON_BYTES) or b"{}")
            except ValueError:
                payload = {"detail": "The backup could not be read."}
            connection.close()
            return SchedulerResponse(status=response.status, payload=payload), None

        def chunks() -> Iterator[bytes]:
            try:
                while chunk := response.read(CHUNK_SIZE):
                    yield chunk
            finally:
                connection.close()

        meta = {
            "filename": _filename(response.getheader("Content-Disposition")),
            "sha256": response.getheader("X-Backup-Sha256"),
            "length": response.getheader("Content-Length"),
        }
        return SchedulerResponse(status=200, payload=meta), chunks()


def _filename(disposition: str | None) -> str:
    text = str(disposition or "")
    marker = 'filename="'
    if marker in text:
        candidate = text.split(marker, 1)[1].split('"', 1)[0]
        if candidate and "/" not in candidate and "\\" not in candidate:
            return candidate
    return "backup.archive"
