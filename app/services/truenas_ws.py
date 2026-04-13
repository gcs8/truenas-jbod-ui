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
            enclosures = await self._call(ws, "enclosure.query", [])
            try:
                disks = await self._call(ws, "disk.query", [[], {"extra": {"pools": True}}])
            except TrueNASAPIError:
                logger.warning("disk.query with extra.pools failed; retrying without extra options.")
                disks = await self._call(ws, "disk.query", [[]])
            pools = await self._call(ws, "pool.query", [])
            disk_temperatures: dict[str, int] = {}
            smart_test_results: list[dict[str, Any]] = []
            try:
                temperatures = await self._call(ws, "disk.temperatures", [[]])
                if isinstance(temperatures, dict):
                    disk_temperatures = {
                        str(key): value
                        for key, value in temperatures.items()
                        if isinstance(value, int)
                    }
            except TrueNASAPIError:
                logger.warning("disk.temperatures failed; continuing without temperature overview.")
            try:
                results = await self._call(ws, "smart.test.results", [])
                if isinstance(results, list):
                    smart_test_results = [item for item in results if isinstance(item, dict)]
            except TrueNASAPIError:
                logger.warning("smart.test.results failed; continuing without SMART test overview.")
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
            result = await self._call(ws, "disk.smartctl", [disk_name, command_args])
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
