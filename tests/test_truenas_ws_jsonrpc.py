from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from pydantic import ValidationError

from app.config import TrueNASConfig
from app.services.truenas_ws import (
    TrueNASAPIBusyError,
    TrueNASAPIError,
    TrueNASWebsocketClient,
    build_jsonrpc_url,
    build_websocket_url,
)


DEFAULT_JSONRPC_URL = "wss://jsonrpc.invalid/api/current"

# A trace is the one part of a middleware error that must never reach the UI.
SECRET_TRACE_MARKER = "middleware-internal-frame"


def jsonrpc_config(**overrides) -> TrueNASConfig:
    values = {
        "host": "https://jsonrpc.invalid",
        "api_key": "token",
        "platform": "scale",
        "api_dialect": "jsonrpc",
    }
    values.update(overrides)
    return TrueNASConfig(**values)


class SyntheticJsonRpcPeer:
    """In-memory JSON-RPC 2.0 wire peer; only connect is replaced, not client methods."""

    def __init__(self, *, url: str = DEFAULT_JSONRPC_URL, mode: str = "normal") -> None:
        self.url = url
        self.mode = mode
        self.connections = self.logins = self.closes = 0
        self.sent: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.authenticated = False

    @asynccontextmanager
    async def connect(self, url, **kwargs):
        assert url == self.url, url
        self.connections += 1
        self.authenticated = False
        try:
            await asyncio.sleep(0)
            yield self
        finally:
            self.closes += 1

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        assert message["jsonrpc"] == "2.0", message
        assert "msg" not in message, message
        assert isinstance(message["id"], int) and not isinstance(message["id"], bool), message
        request_id = message["id"]
        method = message["method"]

        if method == "auth.login_with_api_key":
            self.logins += 1
            self.authenticated = self.mode != "auth_failure"
            self.queue.put_nowait({"jsonrpc": "2.0", "id": request_id, "result": self.authenticated})
            return

        assert self.authenticated, "middleware method call before authentication"

        if self.mode == "notification_first":
            self.queue.put_nowait(
                {"jsonrpc": "2.0", "method": "collection_update", "params": {"collection": "disk"}}
            )
        if self.mode == "id_mismatch":
            self.queue.put_nowait({"jsonrpc": "2.0", "id": request_id + 1000, "result": "WRONG RESULT"})
        if self.mode == "method_error":
            self.queue.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32001,
                        "message": "Method call error",
                        "data": {
                            "error": 22,
                            "errname": "EINVAL",
                            "reason": "Slot 3 is not populated",
                            "trace": {"class": "CallError", "frames": [SECRET_TRACE_MARKER]},
                        },
                    },
                }
            )
            return
        if self.mode == "too_many_calls":
            self.queue.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": "Too many concurrent calls",
                        "data": {
                            "error": 11,
                            "errname": "EBUSY",
                            "reason": "Maximum number of concurrent calls exceeded",
                        },
                    },
                }
            )
            return

        self.queue.put_nowait({"jsonrpc": "2.0", "id": request_id, "result": self._result_for(message)})

    def _result_for(self, message: dict):
        if message["method"] == "disk.smartctl":
            return f"SMART {message['params'][0]}"
        return None

    async def recv(self) -> str:
        message = await self.queue.get()
        if isinstance(message, Exception):
            raise message
        return json.dumps(message)


class JsonRpcUrlTests(unittest.TestCase):
    def test_current_version_builds_the_unpinned_api_path(self) -> None:
        self.assertEqual(build_jsonrpc_url("https://truenas.example.test"), "wss://truenas.example.test/api/current")
        self.assertEqual(build_jsonrpc_url("truenas.example.test"), "wss://truenas.example.test/api/current")

    def test_pinned_version_builds_the_versioned_api_path(self) -> None:
        self.assertEqual(
            build_jsonrpc_url("https://truenas.example.test", "v25.10.0"),
            "wss://truenas.example.test/api/v25.10.0",
        )

    def test_plain_http_host_keeps_the_unencrypted_scheme_and_base_path(self) -> None:
        self.assertEqual(
            build_jsonrpc_url("http://truenas.example.test:8080/middleware/"),
            "ws://truenas.example.test:8080/middleware/api/current",
        )

    def test_unsupported_api_version_is_refused_instead_of_building_a_path(self) -> None:
        for version in ("25.10.0", "../../etc", "current/../v1.0.0", "", "latest"):
            with self.subTest(version=version), self.assertRaises(TrueNASAPIError):
                build_jsonrpc_url("https://truenas.example.test", version)

    def test_ddp_url_builder_is_unchanged(self) -> None:
        self.assertEqual(build_websocket_url("https://truenas.example.test"), "wss://truenas.example.test/websocket")


class TrueNASConfigDialectTests(unittest.TestCase):
    def test_the_default_dialect_is_ddp_on_the_current_api_path(self) -> None:
        config = TrueNASConfig()
        self.assertEqual(config.api_dialect, "ddp")
        self.assertEqual(config.api_version, "current")

    def test_a_pinned_api_version_is_accepted(self) -> None:
        self.assertEqual(TrueNASConfig(api_version="v25.10.0").api_version, "v25.10.0")

    def test_an_invalid_api_version_is_rejected(self) -> None:
        for version in ("25.10.0", "v25.10", "latest", "../../etc", "current "):
            with self.subTest(version=version), self.assertRaises(ValidationError):
                TrueNASConfig(api_version=version)

    def test_an_unknown_dialect_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            TrueNASConfig(api_dialect="rest")


class JsonRpcTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_and_one_method_call_use_json_rpc_without_the_ddp_handshake(self) -> None:
        peer = SyntheticJsonRpcPeer()
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            result = await client.fetch_disk_smartctl("sda")

        self.assertEqual(result, "SMART sda")
        self.assertEqual((peer.connections, peer.logins, peer.closes), (1, 1, 1))
        self.assertEqual(
            [message["method"] for message in peer.sent],
            ["auth.login_with_api_key", "disk.smartctl"],
        )
        self.assertEqual(peer.sent[1]["params"], ["sda", ["-a", "-j"]])
        self.assertEqual(len({message["id"] for message in peer.sent}), len(peer.sent))

    async def test_batched_calls_share_one_authenticated_session(self) -> None:
        peer = SyntheticJsonRpcPeer()
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            results = await client.smartctl_batch(["sda", "sdb", "sdc"], max_concurrency=2)

        self.assertEqual(results, ["SMART sda", "SMART sdb", "SMART sdc"])
        self.assertEqual((peer.connections, peer.logins, peer.closes), (1, 1, 1))
        self.assertEqual(len({message["id"] for message in peer.sent}), len(peer.sent))

    async def test_method_error_reports_errname_and_reason_without_the_trace(self) -> None:
        peer = SyntheticJsonRpcPeer(mode="method_error")
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            with self.assertRaises(TrueNASAPIError) as caught:
                await client.set_slot_status("enc-1", 3, "IDENTIFY")

        exception = caught.exception
        self.assertNotIsInstance(exception, TrueNASAPIBusyError)
        self.assertFalse(exception.retryable)
        self.assertEqual(exception.errname, "EINVAL")
        self.assertEqual(exception.reason, "Slot 3 is not populated")
        self.assertEqual(exception.code, -32001)
        self.assertIn("EINVAL", str(exception))
        self.assertIn("Slot 3 is not populated", str(exception))
        self.assertNotIn(SECRET_TRACE_MARKER, str(exception))

    async def test_too_many_concurrent_calls_is_a_distinct_retryable_error(self) -> None:
        peer = SyntheticJsonRpcPeer(mode="too_many_calls")
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            with self.assertRaises(TrueNASAPIBusyError) as caught:
                await client.set_slot_status("enc-1", 3, "IDENTIFY")

        self.assertIsInstance(caught.exception, TrueNASAPIError)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.code, -32000)
        self.assertEqual(caught.exception.errname, "EBUSY")

    async def test_a_mismatched_response_id_is_never_accepted_as_the_result(self) -> None:
        peer = SyntheticJsonRpcPeer(mode="id_mismatch")
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            result = await client.fetch_disk_smartctl("sda")

        self.assertEqual(result, "SMART sda")

    async def test_a_mismatched_response_id_is_ignored_by_the_batch_dispatcher(self) -> None:
        peer = SyntheticJsonRpcPeer(mode="id_mismatch")
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            results = await client.smartctl_batch(["sda", "sdb"], max_concurrency=2)

        self.assertEqual(results, ["SMART sda", "SMART sdb"])

    async def test_notifications_without_an_id_are_ignored(self) -> None:
        peer = SyntheticJsonRpcPeer(mode="notification_first")
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            single = await client.fetch_disk_smartctl("sda")
            batched = await client.smartctl_batch(["sdb"], max_concurrency=1)

        self.assertEqual((single, batched), ("SMART sda", ["SMART sdb"]))

    async def test_failed_authentication_is_reported_instead_of_calling_methods(self) -> None:
        peer = SyntheticJsonRpcPeer(mode="auth_failure")
        client = TrueNASWebsocketClient(jsonrpc_config())

        with patch("app.services.truenas_ws.connect", peer.connect):
            with self.assertRaisesRegex(TrueNASAPIError, "authentication failed"):
                await client.fetch_disk_smartctl("sda")

        self.assertEqual([message["method"] for message in peer.sent], ["auth.login_with_api_key"])

    async def test_a_pinned_api_version_selects_the_versioned_endpoint(self) -> None:
        peer = SyntheticJsonRpcPeer(url="wss://jsonrpc.invalid/api/v25.10.0")
        client = TrueNASWebsocketClient(jsonrpc_config(api_version="v25.10.0"))

        with patch("app.services.truenas_ws.connect", peer.connect):
            self.assertEqual(await client.fetch_disk_smartctl("sda"), "SMART sda")

    async def test_the_ddp_dialect_still_reaches_the_websocket_endpoint(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(host="https://jsonrpc.invalid", api_key="token"))

        self.assertEqual(client._endpoint_url(), "wss://jsonrpc.invalid/websocket")


if __name__ == "__main__":
    unittest.main()
