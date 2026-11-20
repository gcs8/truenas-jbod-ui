"""Correlated DDP and JSON-RPC envelope members are type-checked (Refs #1313).

A reply that answers a pending call but carries an unusable message kind or
error member settles that call at once with the typed ``response_invalid``
failure, in both the single-call readers and the batch dispatchers. In-memory
WebSocket fixtures only; no connection is opened.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from app.config import TrueNASConfig
from app.services.truenas_ws import (
    TrueNASAPIError,
    TrueNASWebsocketClient,
    _JsonRpcCallDispatcher,
    _MiddlewareCallDispatcher,
    build_ddp_error,
    build_jsonrpc_error,
)

# Bounds a wait for a reply the dispatcher should settle at once.
SETTLE_SECONDS = 2.0
WRONG_TYPED_ERRORS: tuple[Any, ...] = ("synthetic refusal", "", [], ["x"], 0, 5, True, 1.5)
WRONG_TYPED_KINDS: tuple[Any, ...] = ([], ["result"], {}, 1, None, True)


class _QueueWS:
    """An in-memory peer: ``send`` records, ``recv`` reads queued frames."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbound: asyncio.Queue[str] = asyncio.Queue()
        self.request_sent = asyncio.Event()

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        if "method" in payload:
            self.request_sent.set()

    async def recv(self) -> str:
        return await self.inbound.get()

    def push(self, message: Any) -> None:
        self.inbound.put_nowait(json.dumps(message))


class DdpDispatcherEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def _reply(self, reply_for: Any, *, before: tuple[Any, ...] = ()) -> Any:
        ws = _QueueWS()
        dispatcher = _MiddlewareCallDispatcher(ws)
        try:
            call = asyncio.create_task(dispatcher.call("disk.query", []))
            await ws.request_sent.wait()
            request_id = ws.sent[-1]["id"]
            for message in before:
                ws.push(message)
            ws.push(reply_for(request_id))
            return await asyncio.wait_for(call, SETTLE_SECONDS)
        finally:
            await dispatcher.close()

    async def test_unusable_message_kind_settles_the_call(self) -> None:
        for kind in WRONG_TYPED_KINDS:
            with self.subTest(kind=kind):
                with self.assertRaises(TrueNASAPIError) as caught:
                    await self._reply(lambda request_id: {"id": request_id, "msg": kind, "result": []})
                self.assertEqual(caught.exception.category, "response_invalid")
        with self.subTest(kind="missing"):
            with self.assertRaises(TrueNASAPIError) as caught:
                await self._reply(lambda request_id: {"id": request_id, "result": []})
            self.assertEqual(caught.exception.category, "response_invalid")

    async def test_unusable_error_member_is_response_invalid(self) -> None:
        for error in WRONG_TYPED_ERRORS:
            with self.subTest(error=error):
                with self.assertRaises(TrueNASAPIError) as caught:
                    await self._reply(lambda request_id: {"id": request_id, "msg": "result", "error": error})
                self.assertEqual(caught.exception.category, "response_invalid")

    async def test_controls_result_object_error_and_unrelated_messages(self) -> None:
        unrelated = (
            {"msg": "added", "collection": "synthetic", "id": "other", "fields": {}},
            {"msg": "result", "id": "another-call", "error": "not ours"},
            {"msg": "nosub", "id": 7},
            {"msg": "ping"},
        )
        result = await self._reply(lambda request_id: {"id": request_id, "msg": "result", "result": [{"name": "da0"}]},
                                   before=unrelated)
        self.assertEqual(result, [{"name": "da0"}])
        # A method may return a non-object result; that is not an envelope fault.
        self.assertEqual(await self._reply(lambda request_id: {"id": request_id, "msg": "result", "result": 0}), 0)
        with self.assertRaises(TrueNASAPIError) as caught:
            await self._reply(lambda request_id: {"id": request_id, "msg": "result", "error": {"error": 5}})
        self.assertEqual(caught.exception.category, "remote_error")


class DdpSingleCallEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def _await(self, *messages: dict[str, Any]) -> Any:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token", platform="core"))
        ws = _QueueWS()
        for message in messages:
            ws.push(message)
        return await asyncio.wait_for(client._await_ddp_reply(ws, "disk.query", "call-1"), SETTLE_SECONDS)

    async def test_unusable_members_are_response_invalid(self) -> None:
        for error in WRONG_TYPED_ERRORS:
            with self.subTest(error=error):
                with self.assertRaises(TrueNASAPIError) as caught:
                    await self._await({"id": "call-1", "msg": "result", "error": error})
                self.assertEqual(caught.exception.category, "response_invalid")
        for kind in WRONG_TYPED_KINDS:
            with self.subTest(kind=kind):
                with self.assertRaises(TrueNASAPIError) as caught:
                    await self._await({"id": "call-1", "msg": kind})
                self.assertEqual(caught.exception.category, "response_invalid")

    async def test_controls(self) -> None:
        result = await self._await(
            {"msg": "result", "id": "other", "error": "not ours"},
            {"id": "call-1", "msg": "result", "result": [{"name": "da0"}]},
        )
        self.assertEqual(result, [{"name": "da0"}])
        with self.assertRaises(TrueNASAPIError) as caught:
            await self._await({"id": "call-1", "msg": "result", "error": {"error": 5}})
        self.assertEqual(caught.exception.category, "remote_error")


class JsonRpcEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_unusable_error_member_is_response_invalid(self) -> None:
        for error in WRONG_TYPED_ERRORS:
            with self.subTest(error=error):
                ws = _QueueWS()
                dispatcher = _JsonRpcCallDispatcher(ws)
                try:
                    call = asyncio.create_task(dispatcher.call("disk.query", []))
                    await ws.request_sent.wait()
                    ws.push({"jsonrpc": "2.0", "id": ws.sent[-1]["id"], "error": error})
                    with self.assertRaises(TrueNASAPIError) as caught:
                        await asyncio.wait_for(call, SETTLE_SECONDS)
                finally:
                    await dispatcher.close()
                self.assertEqual(caught.exception.category, "response_invalid")

    async def test_single_call_unusable_error_member_is_response_invalid(self) -> None:
        client = TrueNASWebsocketClient(
            TrueNASConfig(host="https://jsonrpc.invalid", api_key="token", platform="scale", api_dialect="jsonrpc")
        )
        for error in WRONG_TYPED_ERRORS:
            with self.subTest(error=error):
                ws = _QueueWS()
                ws.push({"jsonrpc": "2.0", "method": "collection_update", "params": {}})
                ws.push({"jsonrpc": "2.0", "id": 1, "error": error})
                with self.assertRaises(TrueNASAPIError) as caught:
                    await asyncio.wait_for(client._await_jsonrpc_reply(ws, "disk.query", 1), SETTLE_SECONDS)
                self.assertEqual(caught.exception.category, "response_invalid")
        ws = _QueueWS()
        ws.push({"jsonrpc": "2.0", "id": 1, "result": [{"name": "da0"}]})
        self.assertEqual(await client._await_jsonrpc_reply(ws, "disk.query", 1), [{"name": "da0"}])


class ErrorDecoderTests(unittest.TestCase):
    def test_non_object_error_members_are_response_invalid(self) -> None:
        for decoder in (build_ddp_error, build_jsonrpc_error):
            for error in (*WRONG_TYPED_ERRORS, None):
                with self.subTest(decoder=decoder.__name__, error=error):
                    exception = decoder("disk.query", error)
                    self.assertEqual(exception.category, "response_invalid")
                    self.assertEqual(str(exception), "disk.query returned an unusable reply.")

    def test_object_error_members_keep_their_categories(self) -> None:
        self.assertEqual(build_ddp_error("disk.query", {"error": 5}).category, "remote_error")
        self.assertEqual(build_ddp_error("disk.query", {}).category, "remote_error")
        self.assertEqual(build_ddp_error("disk.query", {"errname": "ENOMETHOD"}).category, "method_unavailable")
        self.assertEqual(build_jsonrpc_error("disk.query", {"code": -32601}).category, "method_unavailable")
        self.assertEqual(build_jsonrpc_error("disk.query", {"code": -32603}).category, "remote_error")


if __name__ == "__main__":
    unittest.main()
