from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request

from app.config import TrueNASConfig
from app.services.tls_context import (
    build_tls_client_context,
    resolve_tls_server_name,
    urlopen_with_tls_config,
)
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData

logger = logging.getLogger(__name__)


def build_quantastor_api_base(host: str) -> str:
    if "://" not in host:
        host = f"https://{host}"

    parsed = urlsplit(host)
    path = parsed.path.rstrip("/")
    if not path.endswith("/qstorapi"):
        path = f"{path}/qstorapi" if path else "/qstorapi"
    return urlunsplit((parsed.scheme or "https", parsed.netloc, path, "", ""))


class QuantastorRESTClient:
    """
    Minimal REST client for Quantastor appliance inventory.

    The first-pass adapter is intentionally small: it only calls the endpoints
    needed to build the shared inventory model used by the UI.
    """

    def __init__(self, config: TrueNASConfig) -> None:
        self.config = config

    async def fetch_all(self) -> TrueNASRawData:
        systems, disks, pools, pool_devices, ha_groups, hw_disks, hw_enclosures = await asyncio.gather(
            asyncio.to_thread(self._fetch_required_list, "storageSystemEnum"),
            asyncio.to_thread(self._fetch_required_list, "physicalDiskEnum"),
            asyncio.to_thread(self._fetch_required_list, "storagePoolEnum"),
            asyncio.to_thread(self._fetch_optional_list, "storagePoolDeviceEnum"),
            asyncio.to_thread(self._fetch_optional_list, "haGroupEnum"),
            asyncio.to_thread(self._fetch_optional_list, "hwDiskEnum"),
            asyncio.to_thread(self._fetch_optional_list, "hwEnclosureEnum"),
        )
        return TrueNASRawData(
            enclosures=systems,
            disks=disks,
            pools=pools,
            disk_temperatures={},
            smart_test_results=[],
            systems=systems,
            pool_devices=pool_devices,
            ha_groups=ha_groups,
            hw_disks=hw_disks,
            hw_enclosures=hw_enclosures,
        )

    def _fetch_required_list(self, endpoint: str) -> list[dict[str, Any]]:
        payload = self._request_json(endpoint, {"flags": 0})
        if self._is_error_payload(payload):
            raise TrueNASAPIError(f"Quantastor endpoint {endpoint} returned an API error payload: {payload}")
        rows = self._ensure_list(payload)
        if not rows:
            raise TrueNASAPIError(f"Quantastor endpoint {endpoint} returned no usable rows.")
        return rows

    def _fetch_optional_list(self, endpoint: str) -> list[dict[str, Any]]:
        try:
            payload = self._request_json(endpoint, {"flags": 0})
        except TrueNASAPIError:
            logger.warning("Quantastor endpoint %s failed; continuing without it.", endpoint)
            return []
        if self._is_error_payload(payload):
            logger.warning("Quantastor endpoint %s returned an API error payload; continuing without it.", endpoint)
            return []
        return self._ensure_list(payload)

    def _request_json(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if not self.config.api_user or not self.config.api_password:
            raise TrueNASAPIError("Quantastor API access requires TRUENAS_API_USER and TRUENAS_API_PASSWORD.")

        base_url = build_quantastor_api_base(self.config.host).rstrip("/")
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        target = f"{base_url}/{endpoint}"
        if query:
            target = f"{target}?{query}"

        token = base64.b64encode(f"{self.config.api_user}:{self.config.api_password}".encode("utf-8")).decode("ascii")
        request = Request(
            target,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {token}",
            },
        )

        ssl_context = None
        if target.startswith("https://"):
            ssl_context = build_tls_client_context(self.config)

        try:
            with urlopen_with_tls_config(
                request,
                timeout=self.config.timeout_seconds,
                context=ssl_context,
                server_hostname=resolve_tls_server_name(self.config),
            ) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                payload = response.read().decode(charset)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise TrueNASAPIError(f"Quantastor endpoint {endpoint} failed ({exc.code}): {detail or exc.reason}") from exc
        except URLError as exc:
            raise TrueNASAPIError(f"Quantastor API request to {endpoint} failed: {exc.reason}") from exc

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TrueNASAPIError(f"Quantastor endpoint {endpoint} returned invalid JSON.") from exc

    @staticmethod
    def _ensure_list(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("result", "list", "items", "objects", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if all(isinstance(value, dict) for value in payload.values()):
                return [value for value in payload.values() if isinstance(value, dict)]
            if payload:
                return [payload]
        return []

    @staticmethod
    def _is_error_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if "RestError" in payload:
            return True
        error = payload.get("error")
        if isinstance(error, dict) or isinstance(error, str):
            return True
        status = payload.get("status")
        if isinstance(status, str) and status.lower() == "error":
            return True
        return False
