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
from app.request_context import request_id_headers
from app.services.history_status import project_public_collector_status
from history_service.operation_bounds import (
    ALLOWED_HISTORY_METRICS,
    HistoryBudgetExceeded,
    HistoryRequestShapeError,
    build_history_read_plan,
)


logger = logging.getLogger(__name__)
HISTORY_BACKEND_FAILURE_DETAIL = "History backend request failed; see application logs."
HISTORY_BACKEND_DEGRADED_DETAIL = "History backend is degraded; see history service logs."


class HistoryBackendError(RuntimeError):
    """Base class for history backend request failures."""


class HistoryBackendUnavailableError(HistoryBackendError):
    """The backend could not be reached at all (connection refused, DNS, timeout)."""


class HistoryBackendResponseError(HistoryBackendError):
    """The backend answered, but with an HTTP error or an unusable body."""

    def __init__(self, status_code: int, detail: str | None = None) -> None:
        self.status_code = status_code
        super().__init__(detail or f"History backend returned HTTP {self.status_code}.")


class HistoryBackendBusyError(HistoryBackendResponseError):
    """The history service rejected bulk work at its shared read boundary."""

    def __init__(self) -> None:
        super().__init__(503)


class HistoryBackendPolicyError(HistoryBackendResponseError):
    """The backend rejected a request under its public authorization or budget policy."""


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
        except Exception:  # noqa: BLE001 - surface optional-backend errors as degraded status.
            logger.warning("History backend status request failed.")
            return {
                "configured": True,
                "available": False,
                "detail": HISTORY_BACKEND_FAILURE_DETAIL,
                "counts": {},
                "collector": {},
                "scopes": [],
            }
        collector = project_public_collector_status(
            payload.get("collector"),
            last_error_detail=HISTORY_BACKEND_DEGRADED_DETAIL,
        )
        if payload.get("status") == "degraded" and not collector.get("last_error"):
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
        except Exception:  # noqa: BLE001 - optional backend should degrade gracefully.
            logger.warning("History backend slot history request failed.")
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


    async def get_scopes_history(
        self,
        *,
        scopes: list[dict[str, Any]],
        since: str,
        metrics: list[str],
        event_limit: int,
        metric_limit: int,
    ) -> dict[str, Any]:
        plan = build_history_read_plan(
            scopes=scopes,
            metrics=metrics,
            since=since,
            event_limit=event_limit,
            metric_limit=metric_limit,
        )
        document = {
            "scopes": [
                {
                    "system_id": scope.system_id,
                    "enclosure_id": scope.enclosure_id,
                    "slots": list(scope.slots),
                }
                for scope in plan.scopes
            ],
            "metrics": list(plan.metrics),
            "since": plan.since,
            "event_limit": plan.event_limit,
            "metric_limit": plan.metric_limit,
        }
        if not self.configured:
            available = False
            detail = "History backend is not configured."
        else:
            try:
                return await self._send_json("/api/history/scopes/bundle", document)
            except HistoryBackendPolicyError:
                raise
            except HistoryBackendResponseError:
                raise
            except (HistoryBackendUnavailableError, OSError):
                logger.warning("History backend multi-scope request failed.")
                available = False
                detail = HISTORY_BACKEND_FAILURE_DETAIL
        return {
            "configured": self.configured,
            "available": available,
            "detail": detail,
            "scopes": [
                {
                    "system_id": scope.system_id,
                    "enclosure_id": scope.enclosure_id,
                    "histories": {
                        str(slot): (
                            self._failed_slot_payload(slot, scope.system_id, scope.enclosure_id)
                            if self.configured
                            else self._unconfigured_slot_payload(slot, scope.system_id, scope.enclosure_id)
                        )
                        for slot in scope.slots
                    },
                }
                for scope in plan.scopes
            ],
            "budget": plan.budget_metadata(),
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
        metrics: list[str] | None = None,
        event_limit: int = 12,
        metric_limit: int = 60,
    ) -> dict[int, dict[str, Any]]:
        if not slots:
            return {}
        if not self.configured:
            return {
                slot: self._unconfigured_slot_payload(slot, system_id, enclosure_id)
                for slot in slots
            }
        since = self._build_since_isoformat(window_hours)
        if since is None:
            raise ValueError("Bulk history reads require window_hours from 1 to 8760.")
        selected_metrics = metrics or list(ALLOWED_HISTORY_METRICS)
        scopes = [{"system_id": system_id or "", "enclosure_id": enclosure_id, "slots": slots}]
        try:
            payload = await self.get_scopes_history(
                scopes=scopes,
                since=since,
                metrics=selected_metrics,
                event_limit=event_limit,
                metric_limit=metric_limit,
            )
            scope_payloads = payload.get("scopes")
            if not isinstance(scope_payloads, list) or len(scope_payloads) != 1:
                raise HistoryBackendResponseError(0, "History backend returned a malformed scope payload.")
            histories = scope_payloads[0].get("histories") if isinstance(scope_payloads[0], dict) else None
            if not isinstance(histories, dict):
                raise HistoryBackendResponseError(0, "History backend returned a malformed histories payload.")
        except HistoryBackendPolicyError:
            raise
        except HistoryBackendResponseError as exc:
            if exc.status_code != 404:
                raise
            payload = await self._fetch_json(
                "/api/history/scopes/slots",
                params={
                    "system_id": system_id,
                    "enclosure_id": enclosure_id,
                    "slots": slots,
                    "since": since,
                    "event_limit": event_limit,
                    "metric_limit": metric_limit,
                    "metrics": selected_metrics,
                },
            )
            histories = payload.get("histories")
            if not isinstance(histories, dict):
                raise HistoryBackendResponseError(0, "History backend returned a malformed histories payload.")
        except (HistoryBudgetExceeded, HistoryRequestShapeError) as exc:
            raise ValueError(str(exc)) from exc
        except HistoryBackendUnavailableError:
            logger.warning("History backend scope history request failed.")
            return {
                slot: self._failed_slot_payload(slot, system_id, enclosure_id)
                for slot in dict.fromkeys(slots)
            }

        normalized: dict[int, dict[str, Any]] = {}
        for slot in dict.fromkeys(slots):
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
    def _slot_payload(
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
        *,
        configured: bool,
        available: bool,
        detail: str | None,
    ) -> dict[str, Any]:
        """The one empty slot history shape every unavailable answer uses."""
        return {
            "configured": configured,
            "available": available,
            "detail": detail,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {},
            "events": [],
            "sample_counts": {},
            "latest_values": {},
            "disk_history": {},
        }

    @classmethod
    def _failed_slot_payload(
        cls,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, Any]:
        return cls._slot_payload(
            slot,
            system_id,
            enclosure_id,
            configured=True,
            available=False,
            detail=HISTORY_BACKEND_FAILURE_DETAIL,
        )

    @classmethod
    def _unconfigured_slot_payload(
        cls,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, Any]:
        return cls._slot_payload(
            slot,
            system_id,
            enclosure_id,
            configured=False,
            available=False,
            detail="History backend is not configured.",
        )

    async def _fetch_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch_json_sync, path, params or {})

    async def _send_json(self, path: str, document: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.refresh_token is not None:
            headers["Authorization"] = f"Bearer {self.config.refresh_token.get_secret_value()}"
        payload_bytes, _ = await asyncio.to_thread(
            self._request_bytes_sync,
            path,
            method="POST",
            body=body,
            headers=headers,
        )
        try:
            payload = json.loads(payload_bytes)
        except json.JSONDecodeError as exc:
            raise HistoryBackendResponseError(0, "History backend returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise HistoryBackendResponseError(0, "History backend returned a non-object JSON payload.")
        return payload

    async def refresh(self, mode: str) -> dict[str, Any]:
        if mode not in {"fast", "full"}:
            raise ValueError("History refresh mode must be fast or full.")
        if not self.configured:
            raise HistoryBackendUnavailableError("History backend is not configured.")
        return await self._send_json("/api/history/refresh", {"mode": mode})

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

        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=request_id_headers(headers),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                raise HistoryBackendBusyError() from exc
            if exc.code in {401, 403, 413, 422, 429}:
                raise HistoryBackendPolicyError(exc.code) from exc
            raise HistoryBackendResponseError(exc.code) from exc
        except urllib.error.URLError as exc:
            raise HistoryBackendUnavailableError(f"History backend request failed: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise HistoryBackendUnavailableError("History backend request timed out.") from exc
