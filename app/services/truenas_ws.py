from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect

from app.config import TrueNASConfig
from app.services.tls_context import build_tls_client_context, resolve_tls_server_name

logger = logging.getLogger(__name__)


class TrueNASAPIError(RuntimeError):
    pass


MethodCaller = Callable[[str, list[Any]], Awaitable[Any]]


class _MiddlewareCallDispatcher:
    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self._pending: dict[str, tuple[str, asyncio.Future[Any]]] = {}
        self._reader_task = asyncio.create_task(self._reader())

    async def call(self, method: str, params: list[Any]) -> Any:
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = (method, future)
        payload = {
            "id": request_id,
            "msg": "method",
            "method": method,
            "params": params,
        }
        logger.debug("Calling TrueNAS websocket method %s", method)
        await self.ws.send(json.dumps(payload))
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def close(self) -> None:
        if self._reader_task.done():
            exc = self._reader_task.exception()
            if exc is not None:
                raise exc
            return
        self._reader_task.cancel()
        try:
            await self._reader_task
        except asyncio.CancelledError:
            pass

    async def _reader(self) -> None:
        try:
            while True:
                raw_message = await self.ws.recv()
                message = json.loads(raw_message)
                if message.get("msg") != "result":
                    continue
                request_id = message.get("id")
                if not isinstance(request_id, str):
                    continue
                pending = self._pending.get(request_id)
                if pending is None:
                    continue
                method, future = pending
                if future.done():
                    continue
                if message.get("error"):
                    future.set_exception(TrueNASAPIError(f"{method} failed: {message['error']}"))
                else:
                    future.set_result(message.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for _method, future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
            raise


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
    systems: list[dict[str, Any]] = field(default_factory=list)
    pool_devices: list[dict[str, Any]] = field(default_factory=list)
    ha_groups: list[dict[str, Any]] = field(default_factory=list)
    hw_disks: list[dict[str, Any]] = field(default_factory=list)
    hw_enclosures: list[dict[str, Any]] = field(default_factory=list)
    cli_disks: list[dict[str, Any]] = field(default_factory=list)
    cli_hw_disks: list[dict[str, Any]] = field(default_factory=list)
    cli_hw_enclosures: list[dict[str, Any]] = field(default_factory=list)


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
            dispatcher = _MiddlewareCallDispatcher(ws)
            try:
                enclosures, disks, pools, disk_temperatures, smart_test_results = await asyncio.gather(
                    self._fetch_enclosures(dispatcher.call),
                    self._fetch_disks(dispatcher.call),
                    self._fetch_pools(dispatcher.call),
                    self._fetch_disk_temperatures(dispatcher.call),
                    self._fetch_smart_test_results(dispatcher.call),
                )
            finally:
                await dispatcher.close()
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
        return build_tls_client_context(self.config)

    async def _perform_handshake(self, ws: ClientConnection) -> None:
        await ws.send(json.dumps({"msg": "connect", "version": "1", "support": ["1"]}))
        response = json.loads(await ws.recv())
        if response.get("msg") != "connected":
            raise TrueNASAPIError(f"Unexpected websocket handshake response: {response}")

        authenticated = await self._call(ws, "auth.login_with_api_key", [self.config.api_key])
        if authenticated is not True:
            raise TrueNASAPIError("TrueNAS API authentication failed.")

    async def _fetch_enclosures(self, call_method: MethodCaller) -> list[dict[str, Any]]:
        methods = ["enclosure.query", "enclosure2.query"]
        if self.config.platform == "scale":
            methods = ["enclosure2.query", "enclosure.query"]

        for method in methods:
            try:
                result = await call_method(method, [])
            except TrueNASAPIError:
                continue
            return self._ensure_list(result)

        logger.warning("No supported enclosure query method succeeded; continuing without enclosure rows.")
        return []

    async def _fetch_disks(self, call_method: MethodCaller) -> list[dict[str, Any]]:
        try:
            query_disks = self._ensure_list(await call_method("disk.query", [[], {"extra": {"pools": True}}]))
        except TrueNASAPIError:
            logger.warning("disk.query with extra.pools failed; retrying without extra options.")
            query_disks = self._ensure_list(await call_method("disk.query", [[]]))

        if self.config.platform != "scale":
            return query_disks

        try:
            details_payload = await call_method("disk.details", [])
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

    async def _fetch_pools(self, call_method: MethodCaller) -> list[dict[str, Any]]:
        return self._ensure_list(await call_method("pool.query", []))

    async def _fetch_disk_temperatures(self, call_method: MethodCaller) -> dict[str, int]:
        try:
            temperatures = await call_method("disk.temperatures", [[]])
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

    async def _fetch_smart_test_results(self, call_method: MethodCaller) -> list[dict[str, Any]]:
        try:
            results = await call_method("smart.test.results", [])
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
            server_hostname=resolve_tls_server_name(self.config) if ssl_context else None,
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
