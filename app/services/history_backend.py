from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.config import HistoryConfig


METRIC_LIMITS: dict[str, int] = {
    "temperature_c": 96,
    "bytes_read": 60,
    "bytes_written": 60,
    "annualized_bytes_written": 60,
    "power_on_hours": 60,
}


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
            payload = await self._fetch_json("/api/history/overview")
        except Exception as exc:  # noqa: BLE001 - surface optional-backend errors as degraded status.
            return {
                "configured": True,
                "available": False,
                "detail": str(exc),
                "counts": {},
                "collector": {},
                "scopes": [],
            }

        return {
            "configured": True,
            "available": True,
            "detail": None,
            "counts": payload.get("counts", {}),
            "collector": payload.get("collector", {}),
            "scopes": payload.get("scopes", []),
        }

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
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

        params = {
            "system_id": system_id,
            "enclosure_id": enclosure_id,
        }
        metric_names = tuple(METRIC_LIMITS.keys())
        try:
            events_payload, *metric_payloads = await asyncio.gather(
                self._fetch_json(
                    f"/api/history/slots/{slot}/events",
                    params={**params, "limit": 12},
                ),
                *[
                    self._fetch_json(
                        f"/api/history/slots/{slot}/metrics",
                        params={
                            **params,
                            "metric_name": metric_name,
                            "limit": limit,
                        },
                    )
                    for metric_name, limit in METRIC_LIMITS.items()
                ],
            )
        except Exception as exc:  # noqa: BLE001 - optional backend should degrade gracefully.
            return {
                "configured": True,
                "available": False,
                "detail": str(exc),
                "slot": slot,
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "metrics": {},
                "events": [],
                "sample_counts": {},
                "latest_values": {},
            }

        metrics = {
            metric_name: payload.get("samples", [])
            for metric_name, payload in zip(metric_names, metric_payloads, strict=False)
        }
        latest_values = {}
        sample_counts = {}
        for metric_name, samples in metrics.items():
            latest_values[metric_name] = samples[0].get("value") if samples else None
            sample_counts[metric_name] = len(samples)

        return {
            "configured": True,
            "available": True,
            "detail": None,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": metrics,
            "events": events_payload.get("events", []),
            "sample_counts": sample_counts,
            "latest_values": latest_values,
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
    ) -> dict[int, dict[str, Any]]:
        if not slots:
            return {}
        if not self.configured:
            return {
                slot: self._unconfigured_slot_payload(slot, system_id, enclosure_id)
                for slot in slots
            }

        try:
            payload = await self._fetch_json(
                "/api/history/scopes/slots",
                params={
                    "system_id": system_id,
                    "enclosure_id": enclosure_id,
                    "slots": slots,
                    "event_limit": 12,
                },
            )
        except Exception:
            return {
                slot: history
                for slot, history in zip(
                    slots,
                    await asyncio.gather(*(self.get_slot_history(slot, system_id, enclosure_id) for slot in slots)),
                    strict=False,
                )
            }

        histories = payload.get("histories")
        if not isinstance(histories, dict):
            return {
                slot: history
                for slot, history in zip(
                    slots,
                    await asyncio.gather(*(self.get_slot_history(slot, system_id, enclosure_id) for slot in slots)),
                    strict=False,
                )
            }

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
                }
        return normalized

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
        }

    async def _fetch_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch_json_sync, path, params or {})

    def _fetch_json_sync(
        self,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        filtered_params = {
            key: value
            for key, value in params.items()
            if value not in {None, ""}
        }
        query = urllib.parse.urlencode(filtered_params, doseq=True)
        url = f"{self.config.service_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"History backend returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"History backend request failed: {exc.reason}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("History backend returned a non-object JSON payload.")
        return payload
