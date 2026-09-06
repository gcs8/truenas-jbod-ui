from __future__ import annotations

import asyncio
import hmac
import json
import math
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException, Request

from app.http_auth import origin_identity, request_origin_allowed
from history_service.config import HistorySettings

MAX_REFRESH_REQUEST_BYTES = 256


@dataclass(frozen=True)
class RefreshAdmissionResult:
    accepted: bool
    status_code: int | None = None
    retry_after: int | None = None


class ManualRefreshAdmission:
    def __init__(
        self,
        *,
        cooldown_seconds: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.monotonic = monotonic
        self._state_lock = asyncio.Lock()
        self._running = False
        self._last_full_started_at: float | None = None

    async def try_acquire(self, mode: str) -> RefreshAdmissionResult:
        async with self._state_lock:
            if self._running:
                return RefreshAdmissionResult(False, status_code=409)
            now = self.monotonic()
            if mode == "full" and self._last_full_started_at is not None:
                remaining = self.cooldown_seconds - (now - self._last_full_started_at)
                if remaining > 0:
                    return RefreshAdmissionResult(
                        False,
                        status_code=429,
                        retry_after=max(1, math.ceil(remaining)),
                    )
            self._running = True
            if mode == "full":
                self._last_full_started_at = now
            return RefreshAdmissionResult(True)

    async def release(self) -> None:
        async with self._state_lock:
            self._running = False


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


async def read_refresh_document(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=422, detail="Refresh request must use application/json.")
    body = await read_limited_request_body(
        request,
        limit=MAX_REFRESH_REQUEST_BYTES,
        detail=f"Refresh request exceeds {MAX_REFRESH_REQUEST_BYTES} bytes.",
    )
    try:
        payload = json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Refresh request must be valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"mode"} or payload["mode"] not in {"fast", "full"}:
        raise HTTPException(status_code=422, detail="Refresh request must contain exactly mode 'fast' or 'full'.")
    return str(payload["mode"])


async def read_limited_request_body(request: Request, *, limit: int, detail: str) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(status_code=413, detail=detail)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid Content-Length header.") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=detail)
        chunks.append(chunk)
    return b"".join(chunks)


def _request_self_origin(request: Request) -> tuple[str, str, int] | None:
    host = request.url.hostname
    if host is None:
        return None
    port = request.url.port
    if port is None:
        port = 443 if request.url.scheme == "https" else 80
    return request.url.scheme.lower(), host.lower(), port


def authorize_refresh_request(request: Request, settings: HistorySettings) -> None:
    supplied_origins = request.headers.getlist("origin") + request.headers.getlist("referer")
    if settings.public_origin:
        origin_allowed = request_origin_allowed(request, settings.public_origin)
    else:
        self_origin = _request_self_origin(request)
        origin_allowed = not supplied_origins or (
            self_origin is not None
            and all(origin_identity(candidate) == self_origin for candidate in supplied_origins)
        )
    if not origin_allowed:
        raise HTTPException(status_code=403, detail="Cross-origin history refresh rejected.")

    if settings.refresh_auth_mode == "network":
        return
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    expected = settings.refresh_token.get_secret_value() if settings.refresh_token is not None else ""
    if separator != " " or scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="History refresh authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
