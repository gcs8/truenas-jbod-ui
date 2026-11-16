"""Admin-side client for the backup scheduler's Unix-socket API.

The admin sidecar authenticates and origin-checks the public
``/api/admin/backups/*`` routes, then forwards them here. The socket lives in a
directory only the scheduler and admin sidecars mount
(``BACKUP_SCHEDULER_SOCKET``, default ``/app/backup-api/scheduler.sock``).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_SOCKET = "/app/backup-api/scheduler.sock"
DEFAULT_TIMEOUT_SECONDS = 30.0
DOWNLOAD_TIMEOUT_SECONDS = 600.0
# A clean-up apply reads kept copies back before it deletes (#1188); a remote
# FULL can take minutes. Both numbers are elapsed-time deadlines for the whole
# exchange (connect, send, answer headers and body), not socket timeouts: the
# apply ends by 10 minutes, and the one status read after a lost answer gets 30
# seconds of its own. The admin service therefore answers at least four minutes
# before the page's own 15-minute timer for this action (LONG_TIMEOUT_MS in
# admin_backups.js) gives up.
LIFECYCLE_APPLY_DEADLINE_SECONDS = 600.0
CLEANUP_STATUS_READ_DEADLINE_SECONDS = 30.0
MAX_JSON_BYTES = 16 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


class SchedulerUnavailableError(RuntimeError):
    """The scheduler socket is missing or not answering."""


class SchedulerNoAnswerError(SchedulerUnavailableError):
    """The whole request was sent, but no usable answer came back.

    This says nothing about what the scheduler did with the request: it may
    have finished, failed or still be working on it. Callers that do not
    handle this separately still see it as ``SchedulerUnavailableError``.
    """


class SchedulerTimeoutError(SchedulerNoAnswerError):
    """The whole request was sent, but its answer did not arrive in time."""


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
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> SchedulerResponse:
        """Send one request and read its JSON answer.

        ``timeout`` bounds each blocking step on its own. ``deadline`` is a
        ``time.monotonic()`` instant by which the whole exchange ends: when it
        passes, the socket is shut down, so a slow or stalled answer cannot
        stretch the wait step by step.
        """

        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SchedulerUnavailableError("The backup scheduler service was not asked in time.")
            timeout = remaining if timeout is None else min(timeout, remaining)
        connection = self._connection(timeout)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if actor:
            headers["X-Backup-Actor"] = actor
        expired = threading.Event()
        opened: list[socket.socket] = []
        watchdog = None
        if deadline is not None:

            def expire() -> None:
                expired.set()
                # The socket kept from connect: http.client lets go of
                # connection.sock once a closing answer's headers are in.
                for sock in opened:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            watchdog = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
            watchdog.daemon = True
            watchdog.start()
        try:
            try:
                connection.connect()
                opened.append(connection.sock)
                if expired.is_set():
                    raise TimeoutError("The deadline passed before the request was sent.")
                connection.request(method, path, body=data, headers=headers)
            except OSError as exc:
                # The send did not complete, so the request is not whole.
                raise SchedulerUnavailableError("The backup scheduler service did not answer.") from exc
            # The whole request was sent: from here on a failure means only that
            # no usable answer came back, not that the scheduler did nothing.
            try:
                response = connection.getresponse()
                raw = response.read(MAX_JSON_BYTES + 1)
            except TimeoutError as exc:
                raise SchedulerTimeoutError("The backup scheduler did not answer in time.") from exc
            except (OSError, http.client.HTTPException) as exc:
                if expired.is_set():
                    raise SchedulerTimeoutError("The backup scheduler did not answer in time.") from exc
                raise SchedulerNoAnswerError("The backup scheduler service did not answer.") from exc
        finally:
            if watchdog is not None:
                watchdog.cancel()
            connection.close()
        if len(raw) > MAX_JSON_BYTES:
            raise SchedulerNoAnswerError("The backup scheduler answer was too large.")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise SchedulerNoAnswerError("The backup scheduler returned an unreadable answer.") from exc
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
            digest = hashlib.sha256()
            received = 0
            while chunk := response.read(CHUNK_SIZE):
                destination.write(chunk)
                digest.update(chunk)
                received += len(chunk)
            expected_length = response.getheader("Content-Length")
            expected_sha256 = response.getheader("X-Backup-Sha256")
            if (expected_length is not None and expected_length.strip() != str(received)) or (
                expected_sha256 and expected_sha256.strip().lower() != digest.hexdigest()
            ):
                return SchedulerResponse(
                    status=502,
                    payload={"detail": "The downloaded backup did not match its recorded checksum."},
                )
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
