from __future__ import annotations

import json
import logging
import ssl
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect

from app.config import TrueNASConfig

logger = logging.getLogger(__name__)


class TrueNASAPIError(RuntimeError):
    pass


def build_websocket_url(host: str) -> str:
    if "://" not in host:
        host = f"https://{host}"

    parsed = urlsplit(host)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/")
    path = f"{path}/websocket" if path else "/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


@dataclass(slots=True)
class TrueNASRawData:
    enclosures: list[dict[str, Any]]
    disks: list[dict[str, Any]]
    pools: list[dict[str, Any]]
    disk_temperatures: dict[str, int]
    smart_test_results: list[dict[str, Any]]


class TrueNASWebsocketClient:
    """
    Minimal DDP websocket client for TrueNAS middleware calls.

    TrueNAS CORE and SCALE both expose `auth.login_with_api_key` and method calls
    through the `/websocket` endpoint. We keep the call surface intentionally small:
    only the methods needed by this app live here.
    """

    def __init__(self, config: TrueNASConfig) -> None:
        self.config = config

    async def fetch_all(self) -> TrueNASRawData:
        async with self._session() as ws:
            enclosures = await self._fetch_enclosures(ws)
            disks = await self._fetch_disks(ws)
            pools = await self._call(ws, "pool.query", [])
            disk_temperatures = await self._fetch_disk_temperatures(ws)
            smart_test_results = await self._fetch_smart_test_results(ws)
            return TrueNASRawData(
                enclosures=self._ensure_list(enclosures),
                disks=self._ensure_list(disks),
                pools=self._ensure_list(pools),
                disk_temperatures=disk_temperatures,
                smart_test_results=smart_test_results,
            )

    async def fetch_disk_smartctl(self, disk_name: str, args: list[str] | None = None) -> str:
        command_args = args or ["-a", "-j"]
        async with self._session() as ws:
            try:
                result = await self._call(ws, "disk.smartctl", [disk_name, command_args])
            except TrueNASAPIError as exc:
                if self.config.platform == "scale" and "ENOMETHOD" in str(exc):
                    raise TrueNASAPIError(
                        "Detailed SMART JSON is not available through the SCALE websocket API on this system."
                    ) from exc
                raise
            if not isinstance(result, str):
                raise TrueNASAPIError(f"disk.smartctl returned unexpected payload type for {disk_name!r}.")
            return result

    async def set_slot_status(self, enclosure_id: str, slot_number: int, status: str) -> None:
        async with self._session() as ws:
            await self._call(ws, "enclosure.set_slot_status", [enclosure_id, slot_number, status])

    def _ssl_context(self) -> ssl.SSLContext | None:
        websocket_url = build_websocket_url(self.config.host)
        if not websocket_url.startswith("wss://"):
            return None

        if self.config.verify_ssl:
            return ssl.create_default_context()

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    async def _perform_handshake(self, ws: ClientConnection) -> None:
        await ws.send(json.dumps({"msg": "connect", "version": "1", "support": ["1"]}))
        response = json.loads(await ws.recv())
        if response.get("msg") != "connected":
            raise TrueNASAPIError(f"Unexpected websocket handshake response: {response}")

        authenticated = await self._call(ws, "auth.login_with_api_key", [self.config.api_key])
        if authenticated is not True:
            raise TrueNASAPIError("TrueNAS API authentication failed.")

    async def _fetch_enclosures(self, ws: ClientConnection) -> list[dict[str, Any]]:
        methods = ["enclosure.query", "enclosure2.query"]
        if self.config.platform == "scale":
            methods = ["enclosure2.query", "enclosure.query"]

        for method in methods:
            try:
                result = await self._call(ws, method, [])
            except TrueNASAPIError:
                continue
            return self._ensure_list(result)

        logger.warning("No supported enclosure query method succeeded; continuing without enclosure rows.")
        return []

    async def _fetch_disks(self, ws: ClientConnection) -> list[dict[str, Any]]:
        try:
            query_disks = self._ensure_list(await self._call(ws, "disk.query", [[], {"extra": {"pools": True}}]))
        except TrueNASAPIError:
            logger.warning("disk.query with extra.pools failed; retrying without extra options.")
            query_disks = self._ensure_list(await self._call(ws, "disk.query", [[]]))

        if self.config.platform != "scale":
            return query_disks

        try:
            details_payload = await self._call(ws, "disk.details", [])
        except TrueNASAPIError:
            logger.warning("disk.details failed on SCALE; continuing with disk.query only.")
            return query_disks

        detail_disks = self._flatten_disk_details(details_payload)
        if not detail_disks:
            return query_disks

        detail_index: dict[str, dict[str, Any]] = {}
        for item in detail_disks:
            for key in self._disk_lookup_keys(item):
                detail_index[key] = item

        merged: list[dict[str, Any]] = []
        for disk in query_disks:
            detail = next((detail_index[key] for key in self._disk_lookup_keys(disk) if key in detail_index), None)
            if detail:
                merged.append({**detail, **disk})
            else:
                merged.append(disk)
        return merged

    async def _fetch_disk_temperatures(self, ws: ClientConnection) -> dict[str, int]:
        try:
            temperatures = await self._call(ws, "disk.temperatures", [[]])
        except TrueNASAPIError:
            logger.warning("disk.temperatures failed; continuing without temperature overview.")
            return {}

        if isinstance(temperatures, dict):
            return {
                str(key): value
                for key, value in temperatures.items()
                if isinstance(value, int)
            }
        return {}

    async def _fetch_smart_test_results(self, ws: ClientConnection) -> list[dict[str, Any]]:
        try:
            results = await self._call(ws, "smart.test.results", [])
        except TrueNASAPIError as exc:
            if self.config.platform == "scale" and "ENOMETHOD" in str(exc):
                logger.info("smart.test.results is unavailable on this SCALE system; continuing without SMART history.")
            else:
                logger.warning("smart.test.results failed; continuing without SMART test overview.")
            return []

        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return []

    @staticmethod
    def _flatten_disk_details(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            combined: list[dict[str, Any]] = []
            for bucket in ("used", "unused"):
                rows = payload.get(bucket)
                if isinstance(rows, list):
                    combined.extend(item for item in rows if isinstance(item, dict))
            return combined
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    @staticmethod
    def _disk_lookup_keys(disk: dict[str, Any]) -> set[str]:
        values = (
            disk.get("name"),
            disk.get("devname"),
            disk.get("identifier"),
            disk.get("serial"),
            disk.get("serial_lunid"),
            disk.get("lunid"),
        )
        return {
            str(value).strip().lower()
            for value in values
            if value is not None and str(value).strip()
        }

    @asynccontextmanager
    async def _session(self):
        websocket_url = build_websocket_url(self.config.host)
        ssl_context = self._ssl_context()

        if not self.config.api_key:
            raise TrueNASAPIError("TRUENAS_API_KEY is required for API access.")

        async with connect(
            websocket_url,
            ssl=ssl_context,
            open_timeout=self.config.timeout_seconds,
            close_timeout=self.config.timeout_seconds,
            ping_interval=20,
            ping_timeout=self.config.timeout_seconds,
        ) as ws:
            await self._perform_handshake(ws)
            yield ws

    async def _call(self, ws: ClientConnection, method: str, params: list[Any]) -> Any:
        request_id = str(uuid.uuid4())
        payload = {
            "id": request_id,
            "msg": "method",
            "method": method,
            "params": params,
        }
        logger.debug("Calling TrueNAS websocket method %s", method)
        await ws.send(json.dumps(payload))

        while True:
            raw_message = await ws.recv()
            message = json.loads(raw_message)
            msg_type = message.get("msg")

            if msg_type == "ping":
                await ws.send(json.dumps({"msg": "pong"}))
                continue

            if message.get("id") != request_id:
                continue

            if msg_type == "result":
                if "error" in message:
                    raise TrueNASAPIError(f"{method} failed: {message['error']}")
                return message.get("result")

            raise TrueNASAPIError(f"Unexpected response for {method}: {message}")

    @staticmethod
    def _ensure_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []
