from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import timedelta
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.config import HistoryConfig
from app.models.domain import utcnow


logger = logging.getLogger(__name__)
HISTORY_BACKEND_FAILURE_DETAIL = "History backend request failed; see application logs."
HISTORY_BACKEND_DEGRADED_DETAIL = "History backend is degraded; see history service logs."


class HistoryBackendError(RuntimeError):
    """Base class for history backend request failures."""


class HistoryBackendUnavailableError(HistoryBackendError):
    """The backend could not be reached at all (connection refused, DNS, timeout).

    Distinguished from HTTP-level failures so per-slot fallbacks can stop fanning out
    once the backend is known to be unreachable instead of waiting out one timeout per
    slot.
    """


class HistoryBackendResponseError(HistoryBackendError):
    """The backend answered, but with an HTTP error or an unusable body."""


class HistoryBackendClient:
    def __init__(self, config: HistoryConfig) -> None:
        self.config = config

    @property
    def configured(self) -> bool:
        return bool(str(self.config.service_url or "").strip())

    async def get_status(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "configured": False,
                "available": False,
                "detail": "History backend is not configured.",
                "counts": {},
                "collector": {},
                "scopes": [],
            }

        try:
            payload = await self._fetch_json("/healthz")
        except Exception as exc:  # noqa: BLE001 - surface optional-backend errors as degraded status.
            logger.warning("History backend status request failed: %s", exc)
            return {
                "configured": True,
                "available": False,
                "detail": HISTORY_BACKEND_FAILURE_DETAIL,
                "counts": {},
                "collector": {},
                "scopes": [],
            }
        collector = dict(payload.get("collector", {})) if isinstance(payload.get("collector"), dict) else {}
        if payload.get("status") == "degraded" or collector.get("last_error"):
            collector["last_error"] = HISTORY_BACKEND_DEGRADED_DETAIL
        return {
            "configured": True,
            "available": True,
            "detail": HISTORY_BACKEND_DEGRADED_DETAIL if payload.get("status") == "degraded" else None,
            "counts": payload.get("counts", {}),
            "collector": collector,
            "scopes": payload.get("scopes", []),
        }

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
        window_hours: int | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            return {
                "configured": False,
                "available": False,
                "detail": "History backend is not configured.",
                "slot": slot,
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "metrics": {},
                "events": [],
                "sample_counts": {},
                "latest_values": {},
            }

        try:
            return await self._fetch_slot_history(slot, system_id, enclosure_id, window_hours=window_hours)
        except Exception as exc:  # noqa: BLE001 - optional backend should degrade gracefully.
            logger.warning("History backend slot history request failed: %s", exc)
            return self._failed_slot_payload(slot, system_id, enclosure_id)

    async def _fetch_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
        *,
        window_hours: int | None,
    ) -> dict[str, Any]:
        """Fetch and shape one slot's history bundle; raises on any backend failure."""

        params = {
            "system_id": system_id,
            "enclosure_id": enclosure_id,
        }
        since = self._build_since_isoformat(window_hours)
        if since:
            params["since"] = since
        payload = await self._fetch_json(
            f"/api/history/slots/{slot}/bundle",
            params={**params, "event_limit": 12},
        )
        return {
            "configured": True,
            "available": True,
            "detail": None,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": payload.get("metrics", {}),
            "events": payload.get("events", []),
            "sample_counts": payload.get("sample_counts", {}),
            "latest_values": payload.get("latest_values", {}),
            "disk_history": payload.get("disk_history", {}),
        }

    async def _fallback_scope_history(
        self,
        slots: list[int],
        system_id: str | None,
        enclosure_id: str | None,
        *,
        window_hours: int | None,
    ) -> dict[int, dict[str, Any]]:
        """Per-slot fallback for a failed batched scope call.

        Bounded by ``fallback_max_concurrency`` so a degraded backend cannot saturate the
        default thread pool, deduplicated so repeated slot ids cost one request, and
        short-circuited: once one request proves the backend unreachable, the remaining
        slots are marked unavailable without waiting out a timeout each.
        """

        unique_slots = list(dict.fromkeys(slots))
        limit = max(1, int(self.config.fallback_max_concurrency or 0))
        semaphore = asyncio.Semaphore(limit)
        unreachable = asyncio.Event()

        async def fetch_one(slot: int) -> dict[str, Any]:
            if unreachable.is_set():
                return self._failed_slot_payload(slot, system_id, enclosure_id)
            async with semaphore:
                if unreachable.is_set():
                    return self._failed_slot_payload(slot, system_id, enclosure_id)
                try:
                    return await self._fetch_slot_history(slot, system_id, enclosure_id, window_hours=window_hours)
                except HistoryBackendUnavailableError as exc:
                    if not unreachable.is_set():
                        logger.warning(
                            "History backend unreachable during per-slot fallback; skipping remaining slots: %s",
                            exc,
                        )
                    unreachable.set()
                except Exception as exc:  # noqa: BLE001 - optional backend should degrade gracefully.
                    logger.warning("History backend slot history request failed: %s", exc)
                return self._failed_slot_payload(slot, system_id, enclosure_id)

        results = await asyncio.gather(*(fetch_one(slot) for slot in unique_slots))
        return dict(zip(unique_slots, results, strict=True))

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
        metrics: list[str] | None = None,
        event_limit: int = 12,
    ) -> dict[int, dict[str, Any]]:
        if not slots:
            return {}
        if not self.configured:
            return {
                slot: self._unconfigured_slot_payload(slot, system_id, enclosure_id)
                for slot in slots
            }

        try:
            params: dict[str, Any] = {
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "slots": slots,
                "since": self._build_since_isoformat(window_hours),
                "event_limit": event_limit,
            }
            if metrics:
                params["metrics"] = metrics
            payload = await self._fetch_json(
                "/api/history/scopes/slots",
                params=params,
            )
        except Exception as exc:  # noqa: BLE001 - optional backend should degrade gracefully.
            logger.warning("History backend scope history request failed; falling back per slot: %s", exc)
            return await self._fallback_scope_history(slots, system_id, enclosure_id, window_hours=window_hours)

        histories = payload.get("histories")
        if not isinstance(histories, dict):
            logger.warning("History backend scope history payload was malformed; falling back per slot.")
            return await self._fallback_scope_history(slots, system_id, enclosure_id, window_hours=window_hours)

        normalized: dict[int, dict[str, Any]] = {}
        for slot in slots:
            history = histories.get(str(slot))
            if isinstance(history, dict):
                normalized[slot] = {
                    "configured": True,
                    "available": True,
                    "detail": None,
                    "slot": slot,
                    "system_id": system_id,
                    "enclosure_id": enclosure_id,
                    "metrics": history.get("metrics", {}),
                    "events": history.get("events", []),
                    "sample_counts": history.get("sample_counts", {}),
                    "latest_values": history.get("latest_values", {}),
                    "disk_history": history.get("disk_history", {}),
                }
            else:
                normalized[slot] = {
                    "configured": True,
                    "available": True,
                    "detail": None,
                    "slot": slot,
                    "system_id": system_id,
                    "enclosure_id": enclosure_id,
                    "metrics": {},
                    "events": [],
                    "sample_counts": {},
                    "latest_values": {},
                    "disk_history": {},
                }
        return normalized

    @staticmethod
    def _failed_slot_payload(
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, Any]:
        return {
            "configured": True,
            "available": False,
            "detail": HISTORY_BACKEND_FAILURE_DETAIL,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {},
            "events": [],
            "sample_counts": {},
            "latest_values": {},
        }

    @staticmethod
    def _unconfigured_slot_payload(
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, Any]:
        return {
            "configured": False,
            "available": False,
            "detail": "History backend is not configured.",
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {},
            "events": [],
            "sample_counts": {},
            "latest_values": {},
            "disk_history": {},
        }

    async def _fetch_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch_json_sync, path, params or {})

    @staticmethod
    def _build_since_isoformat(window_hours: int | None) -> str | None:
        if not isinstance(window_hours, int) or window_hours < 1:
            return None
        return (utcnow() - timedelta(hours=window_hours)).isoformat()

    def _fetch_json_sync(
        self,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        payload_bytes, _ = self._request_bytes_sync(path, params=params)
        try:
            payload = json.loads(payload_bytes)
        except json.JSONDecodeError as exc:
            raise HistoryBackendResponseError("History backend returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise HistoryBackendResponseError("History backend returned a non-object JSON payload.")
        return payload

    def _request_bytes_sync(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        filtered_params = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and (not isinstance(value, str) or value != "")
        }
        query = urllib.parse.urlencode(filtered_params, doseq=True)
        url = f"{self.config.service_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HistoryBackendResponseError(f"History backend returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HistoryBackendUnavailableError(f"History backend request failed: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise HistoryBackendUnavailableError("History backend request timed out.") from exc
