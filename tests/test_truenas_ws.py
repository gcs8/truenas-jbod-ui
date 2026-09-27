from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import TrueNASConfig
from app.services.truenas_ws import _MiddlewareCallDispatcher, TrueNASAPIError, TrueNASWebsocketClient


class TrueNASWebsocketClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_disk_query_accepts_the_maximum_supported_inventory(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token", platform="core"))
        disks = [{"name": f"da{index}"} for index in range(4096)]

        result = await client._fetch_disks(AsyncMock(return_value=disks))

        self.assertEqual(result, disks)

    async def test_disk_query_rejects_inventory_above_the_supported_maximum(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token", platform="core"))
        disks = [{"name": f"da{index}"} for index in range(4097)]

        call_method = AsyncMock(return_value=disks)

        with self.assertRaisesRegex(TrueNASAPIError, "4096"):
            await client._fetch_disks(call_method)

        call_method.assert_awaited_once_with("disk.query", [[], {"extra": {"pools": True}}])

    async def test_disk_query_discards_malformed_and_identity_free_rows(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token", platform="core"))
        disk = {"name": "da0", "model": "Synthetic disk"}
        call_method = AsyncMock(return_value=[None, "not-a-disk", {}, {"model": "metadata only"}, disk])

        result = await client._fetch_disks(call_method)

        self.assertEqual(result, [disk])

    async def test_scale_disk_details_preserve_bounded_query_order(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token", platform="scale"))
        call_method = AsyncMock(
            side_effect=[
                [{"name": "sdb", "status": "ONLINE"}, {"name": "sda", "status": "ONLINE"}],
                {"used": [{"name": "sda", "model": "A"}], "unused": [{"name": "sdb", "model": "B"}]},
            ]
        )

        result = await client._fetch_disks(call_method)

        self.assertEqual([disk["name"] for disk in result], ["sdb", "sda"])
        self.assertEqual([disk["model"] for disk in result], ["B", "A"])

    async def test_scale_disk_details_reject_combined_inventory_above_the_supported_maximum(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token", platform="scale"))
        call_method = AsyncMock(
            side_effect=[
                [{"name": "sda"}],
                {
                    "used": [{"name": f"used{index}"} for index in range(2048)],
                    "unused": [{"name": f"unused{index}"} for index in range(2049)],
                },
            ]
        )

        with self.assertRaisesRegex(TrueNASAPIError, "4096"):
            await client._fetch_disks(call_method)

    async def test_enclosure_query_failure_is_not_reported_as_an_empty_success(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token"))
        primary_error = TrueNASAPIError("enclosure.query failed: EPERM")
        call_method = AsyncMock(
            side_effect=[
                primary_error,
                TrueNASAPIError("enclosure2.query failed: ENOMETHOD"),
            ]
        )

        with self.assertRaises(TrueNASAPIError) as caught:
            await client._fetch_enclosures(call_method)

        self.assertIs(caught.exception, primary_error)
        self.assertEqual(
            [call.args for call in call_method.await_args_list],
            [("enclosure.query", []), ("enclosure2.query", [])],
        )

    async def test_enclosure_query_uses_compatibility_fallback_when_it_succeeds(self) -> None:
        client = TrueNASWebsocketClient(TrueNASConfig(api_key="token"))
        call_method = AsyncMock(
            side_effect=[
                TrueNASAPIError("enclosure.query failed: ENOMETHOD"),
                [{"id": "enc-1"}],
            ]
        )

        enclosures = await client._fetch_enclosures(call_method)

        self.assertEqual(enclosures, [{"id": "enc-1"}])

    async def test_dispatcher_cancellation_during_send_retires_pending_future(self) -> None:
        class BlockingSendWS:
            def __init__(self) -> None:
                self.send_started = asyncio.Event()

            async def send(self, _raw_message: str) -> None:
                self.send_started.set()
                await asyncio.Future()

            async def recv(self) -> str:
                await asyncio.Future()

        websocket = BlockingSendWS()
        dispatcher = _MiddlewareCallDispatcher(websocket)
        call_task = asyncio.create_task(dispatcher.call("disk.query", []))
        await websocket.send_started.wait()

        call_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call_task

        self.assertEqual(dispatcher._pending, {})
        await dispatcher.close()

    async def test_dispatcher_close_is_idempotent_after_normal_shutdown(self) -> None:
        class IdleWS:
            async def recv(self) -> str:
                await asyncio.Future()

        dispatcher = _MiddlewareCallDispatcher(IdleWS())

        await dispatcher.close()
        await dispatcher.close()

    async def test_fetch_all_collects_payloads_in_parallel(self) -> None:
        class TrackingClient(TrueNASWebsocketClient):
            def __init__(self) -> None:
                super().__init__(TrueNASConfig(api_key="token"))
                self.active_calls = 0
                self.max_active_calls = 0

            @asynccontextmanager
            async def _session(self):
                class DummyWS:
                    async def send(self, _payload):
                        return None

                    async def recv(self):
                        await asyncio.Future()

                yield DummyWS()

            async def _track(self, result):
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)
                try:
                    await asyncio.sleep(0.01)
                    return result
                finally:
                    self.active_calls -= 1

            async def _fetch_enclosures(self, _call_method):
                return await self._track([{"id": "enc-1"}])

            async def _fetch_disks(self, _call_method):
                return await self._track([{"name": "da0"}])

            async def _fetch_pools(self, _call_method):
                return await self._track([{"name": "tank"}])

            async def _fetch_disk_temperatures(self, _call_method):
                return await self._track({"da0": 30})

            async def _fetch_smart_test_results(self, _call_method):
                return await self._track([{"disk": "da0", "status": "SUCCESS"}])

        client = TrackingClient()

        payload = await client.fetch_all()

        self.assertEqual(client.max_active_calls, 5)
        self.assertEqual(payload.enclosures[0]["id"], "enc-1")
        self.assertEqual(payload.disks[0]["name"], "da0")
        self.assertEqual(payload.pools[0]["name"], "tank")
        self.assertEqual(payload.disk_temperatures["da0"], 30)
        self.assertEqual(payload.smart_test_results[0]["status"], "SUCCESS")

    async def test_fetch_all_retains_other_payloads_when_only_enclosures_fail(self) -> None:
        class EnclosureFailureClient(TrueNASWebsocketClient):
            @asynccontextmanager
            async def _session(self):
                class DummyWS:
                    async def send(self, _payload):
                        return None

                    async def recv(self):
                        await asyncio.Future()

                yield DummyWS()

            async def _fetch_enclosures(self, _call_method):
                raise TrueNASAPIError("enclosure.query failed: EPERM")

            async def _fetch_disks(self, _call_method):
                return [{"name": "da0"}]

            async def _fetch_pools(self, _call_method):
                return [{"name": "tank"}]

            async def _fetch_disk_temperatures(self, _call_method):
                return {"da0": 30}

            async def _fetch_smart_test_results(self, _call_method):
                return [{"disk": "da0", "status": "SUCCESS"}]

        payload = await EnclosureFailureClient(TrueNASConfig(api_key="token")).fetch_all()

        self.assertEqual(payload.enclosures, [])
        self.assertTrue(payload.enclosure_query_failed)
        self.assertEqual(payload.disks, [{"name": "da0"}])
        self.assertEqual(payload.pools, [{"name": "tank"}])
        self.assertEqual(payload.disk_temperatures, {"da0": 30})
        self.assertEqual(payload.smart_test_results, [{"disk": "da0", "status": "SUCCESS"}])

    async def test_fetch_all_failure_cancels_sibling_calls_instead_of_leaving_them_pending(self) -> None:
        class OneErrorWS:
            """Answers pool.query with a middleware error; every other call never answers."""

            def __init__(self) -> None:
                self.queue: asyncio.Queue[str] = asyncio.Queue()
                self.methods: list[str] = []

            async def send(self, raw_message: str) -> None:
                message = json.loads(raw_message)
                self.methods.append(message["method"])
                if message["method"] == "pool.query":
                    await self.queue.put(
                        json.dumps({"msg": "result", "id": message["id"], "error": {"reason": "EPERM"}})
                    )

            async def recv(self) -> str:
                return await self.queue.get()

        fake_ws = OneErrorWS()

        class OneErrorClient(TrueNASWebsocketClient):
            @asynccontextmanager
            async def _session(self):
                yield fake_ws

        client = OneErrorClient(TrueNASConfig(api_key="token"))
        baseline = set(asyncio.all_tasks())

        with self.assertRaisesRegex(TrueNASAPIError, "pool.query failed"):
            await client.fetch_all()
        await asyncio.sleep(0)

        leftover = [
            task.get_coro().__qualname__
            for task in asyncio.all_tasks() - baseline
            if not task.done()
        ]
        self.assertEqual(leftover, [])
        self.assertEqual(sorted(fake_ws.methods), sorted([
            "disk.query", "disk.temperatures", "enclosure.query", "pool.query", "smart.test.results",
        ]))

    async def test_fetch_all_preserves_fetch_failure_when_reader_also_fails_during_close(self) -> None:
        primary_error = TrueNASAPIError("pool query failed first")
        reader_error = RuntimeError("reader failed during shutdown")

        class ReaderFailsDuringCloseWS:
            async def recv(self) -> str:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    raise reader_error

        class FailingClient(TrueNASWebsocketClient):
            @asynccontextmanager
            async def _session(self):
                yield ReaderFailsDuringCloseWS()

            async def _fetch_pools(self, _call_method):
                raise primary_error

            async def _fetch_enclosures(self, _call_method):
                await asyncio.Future()

            async def _fetch_disks(self, _call_method):
                await asyncio.Future()

            async def _fetch_disk_temperatures(self, _call_method):
                await asyncio.Future()

            async def _fetch_smart_test_results(self, _call_method):
                await asyncio.Future()

        with self.assertRaises(TrueNASAPIError) as caught:
            await FailingClient(TrueNASConfig(api_key="token")).fetch_all()

        self.assertIs(caught.exception, primary_error)

    async def test_fetch_all_preserves_caller_cancellation_when_reader_fails_during_close(self) -> None:
        fetch_started = asyncio.Event()
        reader_error = RuntimeError("reader failed during cancelled shutdown")

        class ReaderFailsDuringCloseWS:
            async def recv(self) -> str:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    raise reader_error

        class BlockingClient(TrueNASWebsocketClient):
            @asynccontextmanager
            async def _session(self):
                yield ReaderFailsDuringCloseWS()

            async def _block(self):
                fetch_started.set()
                await asyncio.Future()

            async def _fetch_enclosures(self, _call_method):
                await self._block()

            async def _fetch_disks(self, _call_method):
                await self._block()

            async def _fetch_pools(self, _call_method):
                await self._block()

            async def _fetch_disk_temperatures(self, _call_method):
                await self._block()

            async def _fetch_smart_test_results(self, _call_method):
                await self._block()

        fetch_task = asyncio.create_task(BlockingClient(TrueNASConfig(api_key="token")).fetch_all())
        await fetch_started.wait()

        fetch_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await fetch_task

    async def test_reader_failure_reaches_fetch_all_callers(self) -> None:
        reader_error = RuntimeError("websocket reader failed")

        class ReaderFailureWS:
            def __init__(self) -> None:
                self.sent_count = 0
                self.all_calls_sent = asyncio.Event()

            async def send(self, _raw_message: str) -> None:
                self.sent_count += 1
                if self.sent_count == 5:
                    self.all_calls_sent.set()

            async def recv(self) -> str:
                await self.all_calls_sent.wait()
                raise reader_error

        class ReaderFailureClient(TrueNASWebsocketClient):
            @asynccontextmanager
            async def _session(self):
                yield ReaderFailureWS()

        with self.assertRaises(RuntimeError) as caught:
            await ReaderFailureClient(TrueNASConfig(api_key="token")).fetch_all()

        self.assertIs(caught.exception, reader_error)

    @patch("app.services.truenas_ws.connect")
    async def test_session_passes_tls_server_name_override_to_connect(self, connect_mock: MagicMock) -> None:
        client = TrueNASWebsocketClient(
            TrueNASConfig(
                host="https://10.13.37.10",
                api_key="token",
                tls_server_name="TrueNAS.gcs8.io",
            )
        )

        websocket = MagicMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = websocket
        cm.__aexit__.return_value = False
        connect_mock.return_value = cm

        with patch.object(client, "_perform_handshake", AsyncMock()):
            async with client._session():
                pass

        self.assertEqual(connect_mock.call_args.kwargs["server_hostname"], "TrueNAS.gcs8.io")


class SyntheticDDPPeer:
    """In-memory wire peer; only connect is replaced, not client methods."""

    def __init__(self, width=1, *, mode="normal", close_error=False):
        self.width = width
        self.mode = mode
        self.close_error = close_error
        self.connections = self.logins = self.closes = 0
        self.active = self.peak = 0
        self.requests = []
        self.replies = []
        self.waiting = []
        self.queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.reader_error = RuntimeError("synthetic reader failure")
        self.reader_tasks = set()
        self.send_tasks = set()
        self.authenticated = False
        self.late_error_raised = False

    @asynccontextmanager
    async def connect(self, url, **kwargs):
        assert url == "wss://smart-batch.invalid/websocket"
        self.connections += 1
        self.authenticated = False
        try:
            await asyncio.sleep(0)
            yield self
        finally:
            self.closes += 1
            self.active = 0
            if self.close_error:
                raise RuntimeError("synthetic close failure")

    async def send(self, raw):
        message = json.loads(raw)
        if message["msg"] == "connect":
            assert message == {"msg": "connect", "version": "1", "support": ["1"]}
            if self.mode != "handshake_timeout":
                self.queue.put_nowait({"msg": "connected"})
            return
        if message["msg"] == "pong":
            return
        method = message["method"]
        if method == "auth.login_with_api_key":
            self.logins += 1
            self.authenticated = self.mode != "auth_failure"
            if self.mode != "auth_timeout":
                self.queue.put_nowait({"msg": "result", "id": message["id"], "result": self.authenticated})
            return
        assert self.authenticated
        if method == "enclosure.set_slot_status":
            self.requests.append(message["params"])
            self.queue.put_nowait({"msg": "result", "id": message["id"], "result": None})
            return
        assert method == "disk.smartctl"
        self.send_tasks.add(asyncio.current_task())
        self.requests.append(message["params"])
        self.waiting.append(message)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        if self.mode in ("send", "send_reader_failure"):
            if self.mode == "send_reader_failure":
                self.queue.put_nowait(self.reader_error)
            await asyncio.Future()
        if self.mode in ("response", "late_reader", "timeout"):
            return
        if self.mode == "reader_failure":
            self.queue.put_nowait(self.reader_error)
            return
        if len(self.waiting) == self.width:
            for request in reversed(self.waiting):
                reply = {"msg": "result", "id": request["id"], "result": request["params"][0]}
                if self.mode in ("invalid", "invalid_late_reader"):
                    reply["result"] = {"not": "SMART text"}
                elif self.mode == "middleware":
                    reply["error"] = {"reason": "ENOMETHOD"}
                self.queue.put_nowait(reply)
            self.waiting.clear()

    async def recv(self):
        self.reader_tasks.add(asyncio.current_task())
        try:
            message = await self.queue.get()
        except asyncio.CancelledError:
            if self.mode in ("late_reader", "invalid_late_reader") and self.authenticated:
                self.closing.set()
                await self.release.wait()
                self.late_error_raised = True
                raise self.reader_error
            raise
        if isinstance(message, Exception):
            raise message
        if message.get("msg") == "result" and isinstance(message.get("result"), str):
            self.active -= 1
            self.replies.append(message["result"])
        return json.dumps(message)


class PingGatedDDPPeer(SyntheticDDPPeer):
    """Release real DDP results only after the exact application JSON pong.

    WS448-1 reviewer reproduction: connect, authenticate, receive SMART, send
    ping, withhold SMART until pong. Transport control frames cannot release it.
    """

    def __init__(self, *, phase="smart", ping=None, width=1, pong_mode="normal"):
        super().__init__(width=width, mode="response")
        self.phase = phase
        self.ping = {"msg": "ping"} if ping is None else ping
        self.expected_pong = {**self.ping, "msg": "pong"}
        self.pong_mode = pong_mode
        self.pongs = []
        self.auth_request = None
        self.pong_started = asyncio.Event()
        self.pong_stopped = asyncio.Event()
        self.receivers = self.peak_receivers = 0

    async def send(self, raw):
        message = json.loads(raw)
        if message["msg"] == "pong":
            self.pongs.append(message)
            assert message == self.expected_pong, "DDP pong must preserve optional ping id exactly"
            self.pong_started.set()
            if self.pong_mode == "blocked":
                try:
                    await asyncio.Future()
                finally:
                    self.pong_stopped.set()
            if self.pong_mode == "failure":
                raise self.reader_error
            if self.pong_mode == "repeat":
                # Yield so a peer's application pings cannot starve test timers.
                await asyncio.sleep(0.001)
                self.queue.put_nowait(self.ping)
                return
            if self.auth_request is not None:
                self.authenticated = True
                self.queue.put_nowait({"msg": "result", "id": self.auth_request["id"], "result": True})
                self.auth_request = None
            else:
                for request in reversed(self.waiting):
                    self.queue.put_nowait({"msg": "result", "id": request["id"],
                                           "result": "ping-ok" if self.width == 1 else request["params"][0]})
                self.waiting.clear()
            return
        if message.get("method") == "auth.login_with_api_key" and self.phase == "auth":
            self.logins += 1
            self.auth_request = message
            self.queue.put_nowait(self.ping)
            return
        await super().send(raw)
        if message.get("method") == "disk.smartctl" and len(self.waiting) == self.width:
            if self.phase == "smart":
                self.queue.put_nowait(self.ping)
            else:
                for request in reversed(self.waiting):
                    self.queue.put_nowait({"msg": "result", "id": request["id"],
                                           "result": "ping-ok" if self.width == 1 else request["params"][0]})
                self.waiting.clear()

    async def recv(self):
        self.receivers += 1
        self.peak_receivers = max(self.peak_receivers, self.receivers)
        try:
            return await super().recv()
        finally:
            self.receivers -= 1


class SmartctlBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TrueNASWebsocketClient(TrueNASConfig(
            host="https://smart-batch.invalid", api_key="synthetic-ephemeral-token", platform="core",
        ))
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: self.loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, old_handler)
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            blocker = patch(target, side_effect=AssertionError("unexpected network access"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.baseline = set(asyncio.all_tasks())

    async def asyncTearDown(self):
        import gc
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        self.assertEqual(self.loop_errors, [])
        self.assertEqual([
            t for t in asyncio.all_tasks() - self.baseline
            if t is not asyncio.current_task() and not t.done()
        ], [])

    async def run_peer(self, peer, disks, *, budget=2, args=None, test_timeout=5):
        with patch("app.services.truenas_ws.connect", peer.connect):
            return await asyncio.wait_for(
                self.client.smartctl_batch(disks, args, max_concurrency=budget), test_timeout,
            )

    async def test_single_call_answers_ddp_ping_control(self):
        peer = PingGatedDDPPeer()
        with patch("app.services.truenas_ws.connect", peer.connect):
            self.assertEqual(await asyncio.wait_for(self.client.fetch_disk_smartctl("a"), 1), "ping-ok")
        self.assertEqual(peer.pongs, [{"msg": "pong"}])
        self.assertEqual((peer.connections, peer.logins, peer.closes), (1, 1, 1))

    async def test_batch_answers_ddp_ping_contract(self):
        self.client.config.timeout_seconds = 0.1
        peer = PingGatedDDPPeer()
        with patch("app.services.truenas_ws.connect", peer.connect):
            try:
                result = await self.client.smartctl_batch(["a"], max_concurrency=1)
            except TimeoutError:
                self.fail(f"batch ignored DDP ping: pongs={len(peer.pongs)}, closes={peer.closes}; "
                          "single-call control succeeds")
        self.assertEqual(result, ["ping-ok"])
        self.assertEqual(peer.pongs, [{"msg": "pong"}])
        self.assertEqual((peer.connections, peer.logins, peer.closes, peer.peak_receivers), (1, 1, 1, 1))

    async def test_ddp_optional_ping_id_during_auth_and_smart_core_and_scale(self):
        self.client.config.timeout_seconds = 0.1
        for platform in ("core", "scale"):
            self.client.config.platform = platform
            for phase in ("auth", "smart"):
                for ping in ({"msg": "ping"}, {"msg": "ping", "id": "synthetic-ping"}, {"msg": "ping", "id": ""}):
                    for batch in (False, True):
                        with self.subTest(platform=platform, phase=phase, ping=ping, batch=batch):
                            peer = PingGatedDDPPeer(phase=phase, ping=ping)
                            with patch("app.services.truenas_ws.connect", peer.connect):
                                operation = (self.client.smartctl_batch(["a"], max_concurrency=1) if batch
                                             else self.client.fetch_disk_smartctl("a"))
                                try:
                                    result = await asyncio.wait_for(operation, 1)
                                except TimeoutError:
                                    self.fail("DDP ping prevented authentication or SMART progress")
                            self.assertEqual(result, ["ping-ok"] if batch else "ping-ok")
                            self.assertEqual(peer.pongs, [{**ping, "msg": "pong"}])
                            self.assertEqual((peer.connections, peer.logins, peer.closes, peer.peak_receivers),
                                             (1, 1, 1, 1))

    async def test_ddp_ping_batch_reorders_results_without_extra_receiver(self):
        peer = PingGatedDDPPeer(width=2, ping={"msg": "ping", "id": "reorder"})
        self.client.config.timeout_seconds = 0.1
        disks = ["a", "b", "c", "d"]
        self.assertEqual(await self.run_peer(peer, disks), disks)
        self.assertEqual(peer.replies, ["b", "a", "d", "c"])
        self.assertEqual(peer.pongs, [{"msg": "pong", "id": "reorder"}] * 2)
        self.assertEqual((peer.connections, peer.logins, peer.closes, peer.peak_receivers, peer.peak),
                         (1, 1, 1, 1, 2))

    async def test_ddp_pong_block_or_repeated_ping_does_not_reset_batch_deadline(self):
        self.client.config.timeout_seconds = 0.03
        for phase in ("auth", "smart"):
            for mode in ("blocked", "repeat"):
                with self.subTest(phase=phase, mode=mode):
                    peer = PingGatedDDPPeer(phase=phase, pong_mode=mode)
                    with patch("app.services.truenas_ws.connect", peer.connect):
                        task = asyncio.create_task(self.client.smartctl_batch(["a"], max_concurrency=1))
                        try:
                            done, _ = await asyncio.wait({task}, timeout=1)
                            self.assertIn(task, done, "application ping extended the configured deadline")
                            with self.assertRaises(TimeoutError):
                                task.result()
                        finally:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                    self.assertTrue(peer.pongs, "test must reach application pong send")
                    self.assertEqual(peer.closes, 1)
                    if mode == "blocked":
                        self.assertTrue(peer.pong_stopped.is_set())

    async def test_ddp_pong_send_failure_preserves_error_and_closes(self):
        self.client.config.timeout_seconds = 0.1
        for phase in ("auth", "smart"):
            with self.subTest(phase=phase):
                peer = PingGatedDDPPeer(phase=phase, pong_mode="failure")
                with self.assertRaises(RuntimeError) as caught:
                    await self.run_peer(peer, ["a"], budget=1)
                self.assertIs(caught.exception, peer.reader_error)
                self.assertEqual(peer.closes, 1)

    async def test_ddp_pong_send_cancellation_drains_auth_and_dispatcher(self):
        for phase in ("auth", "smart"):
            with self.subTest(phase=phase):
                peer = PingGatedDDPPeer(phase=phase, pong_mode="blocked")
                with patch("app.services.truenas_ws.connect", peer.connect):
                    task = asyncio.create_task(self.client.smartctl_batch(["a"], max_concurrency=1))
                    try:
                        await asyncio.wait_for(peer.pong_started.wait(), 1)
                        task.cancel()
                        await asyncio.sleep(0)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 1)
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(peer.pong_stopped.is_set())
                self.assertEqual(peer.closes, 1)

    async def test_single_call_predecessor_counts_separate_sessions(self):
        for size in (60, 84):
            with self.subTest(size=size):
                peer = SyntheticDDPPeer()
                disks = [f"invented{n}" for n in range(size)]
                with patch("app.services.truenas_ws.connect", peer.connect):
                    results = [await self.client.fetch_disk_smartctl(d) for d in disks]
                self.assertEqual(results, disks)
                self.assertEqual((peer.connections, peer.logins, peer.closes, peer.peak), (size, size, size, 1))

    async def test_batch_one_session_bounded_parallelism_and_order(self):
        for size in (60, 84):
            for budget in (1, 2, 12):
                with self.subTest(size=size, budget=budget):
                    peer = SyntheticDDPPeer(width=budget)
                    disks = [f"invented{n}" for n in range(size)]
                    self.assertEqual(await self.run_peer(peer, disks, budget=budget), disks)
                    self.assertEqual((peer.connections, peer.logins, peer.closes, peer.peak), (1, 1, 1, budget))
                    self.assertEqual(len(peer.requests), size)
                    self.assertEqual(peer.active, 0)
                    if budget > 1:
                        self.assertNotEqual(peer.replies, disks)
                    self.assertTrue(all(request[1] == ["-a", "-j"] for request in peer.requests))

    async def test_empty_and_invalid_admission_never_connect(self):
        peer = SyntheticDDPPeer()
        self.assertEqual(await self.run_peer(peer, [], budget=1), [])
        for budget in (0, -1, True, 1.5, "2", None):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                await self.run_peer(peer, ["invented0"], budget=budget)
        for disks in ("invented0", iter(["invented0"]), [None], [""], [1]):
            with self.subTest(disks_type=type(disks).__name__), self.assertRaises(ValueError):
                await self.run_peer(peer, disks)
        with patch("app.services.truenas_ws.connect", peer.connect), self.assertRaises(TypeError):
            await self.client.smartctl_batch(["invented0"])
        with self.assertRaisesRegex(TrueNASAPIError, "4096"):
            await self.run_peer(peer, ["invented0"] * 4097)
        self.assertEqual(peer.connections, 0)

    async def test_exact_limit_duplicates_and_explicit_args(self):
        peer = SyntheticDDPPeer()
        disks = ["invented0"] * 4096
        # This is a cardinality/concurrency contract, not a throughput benchmark.
        # Keep a generous deadlock guard and prove the exact structure below.
        self.assertEqual(
            await self.run_peer(peer, disks, budget=1, args=["-x", "-j"], test_timeout=60),
            disks,
        )
        self.assertEqual((peer.connections, peer.logins, peer.closes, peer.peak), (1, 1, 1, 1))
        self.assertEqual(len(peer.requests), 4096)
        self.assertTrue(all(r == ["invented0", ["-x", "-j"]] for r in peer.requests))
        peer = SyntheticDDPPeer(width=2)
        self.assertEqual(await self.run_peer(peer, ["same", "same"], budget=12, args=[]), ["same", "same"])
        self.assertEqual((len(peer.requests), peer.peak), (2, 2))
        self.assertTrue(all(r[1] == ["-a", "-j"] for r in peer.requests))

    async def test_invalid_middleware_and_reader_failures_close_session(self):
        for mode, error, text in (
            ("invalid", TrueNASAPIError, "unexpected payload type"),
            ("middleware", TrueNASAPIError, "disk.smartctl failed"),
            ("reader_failure", RuntimeError, "synthetic reader failure"),
            ("auth_failure", TrueNASAPIError, "authentication failed"),
        ):
            with self.subTest(mode=mode):
                peer = SyntheticDDPPeer(mode=mode)
                with self.assertRaisesRegex(error, text) as caught:
                    await self.run_peer(peer, ["invented0"] * 20)
                if mode == "reader_failure":
                    self.assertIs(caught.exception, peer.reader_error)
                self.assertEqual((peer.connections, peer.logins, peer.closes), (1, 1, 1))
                self.assertTrue(all(t.done() for t in peer.send_tasks))
        self.client.config.platform = "scale"
        with self.assertRaisesRegex(TrueNASAPIError, "Detailed SMART JSON is not available"):
            await self.run_peer(SyntheticDDPPeer(mode="middleware"), ["invented0"])

    async def test_reader_failure_interrupts_blocked_send_without_waiting_for_deadline(self):
        peer = SyntheticDDPPeer(mode="send_reader_failure")
        with self.assertRaises(RuntimeError) as caught:
            await self.run_peer(peer, ["invented0"] * 20)
        self.assertIs(caught.exception, peer.reader_error)
        self.assertEqual(peer.closes, 1)

    async def test_pending_send_response_handshake_and_auth_have_deadlines(self):
        self.client.config.timeout_seconds = 0.02
        for mode in ("send", "timeout", "handshake_timeout", "auth_timeout", "send_reader_failure"):
            with self.subTest(mode=mode):
                peer = SyntheticDDPPeer(mode=mode)
                with patch("app.services.truenas_ws.connect", peer.connect):
                    task = asyncio.create_task(self.client.smartctl_batch(["invented0"] * 20, max_concurrency=2))
                    try:
                        done, _ = await asyncio.wait({task}, timeout=1)
                        self.assertIn(task, done, "configured per-call deadline did not finish the batch")
                        with self.assertRaises((TimeoutError, RuntimeError)):
                            task.result()
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(peer.closes, 1)
                self.assertTrue(all(t.done() for t in peer.send_tasks))

    async def test_a_stalled_shelf_stops_at_the_phase_deadline(self):
        # #537: 84 stalled disks at width 12 and a 15s per-call timeout used to
        # hold the batch open for roughly 105s. The phase gets a deadline, and
        # positions it never reaches are reported so the caller falls back.
        self.client.config.timeout_seconds = 0.05
        peer = SyntheticDDPPeer(mode="timeout")
        with patch("app.services.truenas_ws.connect", peer.connect):
            results = await asyncio.wait_for(
                self.client.smartctl_batch(
                    ["invented0"] * 20, max_concurrency=2, return_exceptions=True,
                ),
                5,
            )

        self.assertEqual(len(results), 20)
        self.assertTrue(all(isinstance(result, TimeoutError) for result in results))
        not_started = [result for result in results if "batch deadline" in str(result)]
        self.assertGreaterEqual(
            len(not_started),
            16,
            "positions past the phase deadline must not each cost a per-call timeout",
        )

    async def test_cancellation_during_send_and_response_drains_workers(self):
        for mode in ("send", "response"):
            with self.subTest(mode=mode):
                peer = SyntheticDDPPeer(mode=mode)
                with patch("app.services.truenas_ws.connect", peer.connect):
                    task = asyncio.create_task(self.client.smartctl_batch(["invented0"] * 20, max_concurrency=2))
                    await asyncio.wait_for(peer.started.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                self.assertEqual(peer.closes, 1)
                self.assertTrue(all(t.done() for t in peer.send_tasks))

    async def test_repeated_cancellation_waits_for_late_reader_cleanup(self):
        peer = SyntheticDDPPeer(mode="late_reader", close_error=True)
        with patch("app.services.truenas_ws.connect", peer.connect):
            task = asyncio.create_task(self.client.smartctl_batch(["invented0"] * 20, max_concurrency=2))
            try:
                await asyncio.wait_for(peer.started.wait(), 2)
                task.cancel()
                await asyncio.wait_for(peer.closing.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                peer.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
        self.assertEqual(peer.closes, 1)
        self.assertTrue(all(t.done() for t in peer.send_tasks))

    async def test_cancellation_during_failure_cleanup_keeps_session_owned(self):
        peer = SyntheticDDPPeer(mode="invalid_late_reader")
        with patch("app.services.truenas_ws.connect", peer.connect):
            task = asyncio.create_task(self.client.smartctl_batch(["invented0"] * 20, max_concurrency=2))
            try:
                await asyncio.wait_for(peer.closing.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                peer.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
        self.assertEqual(peer.closes, 1)
        self.assertTrue(peer.late_error_raised)

    async def test_authentication_failure_survives_connection_cleanup_failure(self):
        peer = SyntheticDDPPeer(mode="auth_failure", close_error=True)
        with self.assertRaisesRegex(TrueNASAPIError, "authentication failed"):
            await self.run_peer(peer, ["invented0"])
        self.assertEqual(peer.closes, 1)

    async def test_primary_reader_failure_survives_session_close_failure(self):
        peer = SyntheticDDPPeer(mode="reader_failure", close_error=True)
        with self.assertRaises(RuntimeError) as caught:
            await self.run_peer(peer, ["invented0"] * 20)
        self.assertIs(caught.exception, peer.reader_error)
        self.assertEqual(peer.closes, 1)

    async def test_existing_single_call_and_slot_status_contracts(self):
        peer = SyntheticDDPPeer()
        with patch("app.services.truenas_ws.connect", peer.connect):
            self.assertEqual(await self.client.fetch_disk_smartctl("invented0", []), "invented0")
            self.assertIsNone(await self.client.set_slot_status("synthetic-enclosure", 3, "IDENTIFY"))
        self.assertEqual(peer.requests, [["invented0", ["-a", "-j"]], ["synthetic-enclosure", 3, "IDENTIFY"]])
        self.assertEqual((peer.connections, peer.logins, peer.closes), (2, 2, 2))
        self.client.config.platform = "scale"
        with patch("app.services.truenas_ws.connect", SyntheticDDPPeer(mode="middleware").connect):
            with self.assertRaisesRegex(TrueNASAPIError, "Detailed SMART JSON is not available"):
                await self.client.fetch_disk_smartctl("invented0")


class NormalOperationPeer:
    """Synthetic normal-operation wire peer, shared by the two dialect suites."""

    def __init__(self, dialect="ddp", *, phase="method", mode="silence", close_error=False):
        self.dialect, self.phase, self.mode = dialect, phase, mode
        self.close_error = close_error
        self.queue = asyncio.Queue()
        self.sent, self.pongs, self.waiting = [], [], []
        self.connections = self.closes = self.receivers = self.peak_receivers = 0
        self.started, self.closing, self.release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.reader_error = RuntimeError("synthetic normal reader failure")
        self.late_error = RuntimeError("synthetic late transport failure")
        self.late_error_raised = False
        self.authenticated = False
        self.expected_pong = None

    @asynccontextmanager
    async def connect(self, url, **kwargs):
        suffix = "/websocket" if self.dialect == "ddp" else "/api/current"
        assert url == "ws://normal.example.test" + suffix
        self.connections += 1
        try:
            if self.phase == "open":
                self.started.set()
                await asyncio.Future()
            yield self
        finally:
            self.closes += 1
            if self.close_error:
                raise RuntimeError("synthetic normal close failure")

    def reply(self, request, *, error=False):
        result = {
            "auth.login_with_api_key": True,
            "enclosure.query": [{"id": "synthetic-enclosure"}],
            "enclosure2.query": [{"id": "synthetic-enclosure"}],
            "disk.query": [{"name": "da0"}],
            "disk.details": {},
            "pool.query": [{"name": "synthetic-pool"}],
            "disk.temperatures": {"da0": 30},
            "smart.test.results": [{"disk": "da0", "status": "SUCCESS"}],
            "disk.smartctl": "synthetic SMART",
            "enclosure.set_slot_status": None,
        }[request["method"]]
        message = {"id": request["id"], "result": result}
        message.update({"msg": "result"} if self.dialect == "ddp" else {"jsonrpc": "2.0"})
        if error:
            message.pop("result")
            message["error"] = {"code": -32001, "message": "synthetic primary rejection"}
        self.queue.put_nowait(message)

    def ping(self):
        ping = ({"msg": "ping"}, {"msg": "ping", "id": ""},
                {"msg": "ping", "id": "synthetic-ping"})[len(self.pongs) % 3]
        self.expected_pong = {**ping, "msg": "pong"}
        self.queue.put_nowait(ping)

    async def late_failure(self):
        self.closing.set()
        await self.release.wait()
        self.late_error_raised = True
        raise self.late_error

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("msg") == "connect":
            assert self.dialect == "ddp"
            if self.phase == "handshake":
                self.started.set()
            else:
                self.queue.put_nowait({"msg": "connected"})
            return
        if message.get("msg") == "pong":
            assert message == self.expected_pong
            self.pongs.append(message)
            if self.mode == "blocked_pong":
                await asyncio.Future()
            await asyncio.sleep(0.002)
            if self.mode == "ping_success" and len(self.pongs) == 3:
                for request in self.waiting:
                    self.reply(request)
                    if request["method"] == "auth.login_with_api_key":
                        self.mode = "normal"
                self.waiting.clear()
            else:
                self.ping()
            return
        assert isinstance(message["id"], str if self.dialect == "ddp" else int)
        if self.dialect == "jsonrpc":
            assert message["jsonrpc"] == "2.0" and "msg" not in message
        auth = message["method"] == "auth.login_with_api_key"
        if not auth:
            assert self.authenticated, "method before authentication"
        if auth and self.phase != "auth":
            if self.mode == "shared_budget":
                await asyncio.sleep(0.04)
            self.authenticated = True
            self.reply(message)
            return
        self.started.set()
        if self.mode in ("normal", "shared_budget"):
            if self.mode == "shared_budget":
                await asyncio.sleep(0.04)
            self.reply(message)
        elif self.mode in ("ping", "ping_success", "blocked_pong"):
            self.waiting.append(message)
            if self.expected_pong is None:
                self.ping()
        elif self.mode in ("late_send", "reader_during_send"):
            if self.mode == "reader_during_send":
                self.queue.put_nowait(self.reader_error)
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await self.late_failure()
        elif self.mode in ("method_error", "error_late_reader"):
            if message["method"] in ("pool.query", "enclosure.set_slot_status", "disk.smartctl"):
                self.reply(message, error=True)
        elif self.mode == "reader_error":
            self.queue.put_nowait(self.reader_error)

    async def recv(self):
        self.receivers += 1
        self.peak_receivers = max(self.peak_receivers, self.receivers)
        try:
            try:
                message = await self.queue.get()
            except asyncio.CancelledError:
                if self.mode in ("late_reader", "error_late_reader") and self.authenticated:
                    await self.late_failure()
                raise
            if isinstance(message, Exception):
                raise message
            if message.get("result") is True:
                self.authenticated = True
            return json.dumps(message)
        finally:
            self.receivers -= 1


class NormalOperationDeadlineChecks:
    """Run identical public-entry lifecycle assertions for each wire dialect."""

    dialect = "ddp"
    operations = ("fetch_all", "set_slot_status", "fetch_disk_smartctl")

    async def asyncSetUp(self):
        self.client = TrueNASWebsocketClient(TrueNASConfig(
            host="http://normal.example.test", api_key="synthetic-ephemeral-token",
            platform="core" if self.dialect == "ddp" else "scale", api_dialect=self.dialect,
        ))
        self.client.config.timeout_seconds = 0.06
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: self.loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, old_handler)
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            blocker = patch(target, side_effect=AssertionError("unexpected network access"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.baseline = set(asyncio.all_tasks())

    async def asyncTearDown(self):
        import gc
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        self.assertEqual(self.loop_errors, [])
        self.assertEqual([
            task for task in asyncio.all_tasks() - self.baseline
            if task is not asyncio.current_task() and not task.done()
        ], [])

    def operation(self, name):
        if name == "set_slot_status":
            return self.client.set_slot_status("synthetic-enclosure", 3, "IDENTIFY")
        if name == "fetch_disk_smartctl":
            return self.client.fetch_disk_smartctl("da0")
        return self.client.fetch_all()

    @asynccontextmanager
    async def running(self, name, peer):
        with patch("app.services.truenas_ws.connect", peer.connect):
            task = asyncio.create_task(self.operation(name))
            try:
                yield task
            finally:
                peer.release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((peer.connections, peer.closes), (1, 1))
        self.assertLessEqual(peer.peak_receivers, 1)
        self.assertEqual(peer.receivers, 0)

    async def finished(self, task):
        # This watchdog only detects a broken implementation. Its cancellation
        # must never be mistaken for the product's own TimeoutError.
        done, _ = await asyncio.wait({task}, timeout=0.5)
        self.assertIn(task, done, "normal operation exceeded its configured lifetime")

    async def assert_silence(self, phase):
        for name in self.operations:
            with self.subTest(operation=name, phase=phase):
                peer = NormalOperationPeer(self.dialect, phase=phase)
                async with self.running(name, peer) as task:
                    await self.finished(task)
                    with self.assertRaises(TimeoutError):
                        task.result()
                self.assertTrue(peer.started.is_set())
                if phase in ("open", "handshake", "auth"):
                    self.assertFalse(any(m.get("method") not in (None, "auth.login_with_api_key")
                                         for m in peer.sent))

    async def test_open_silence_has_operation_deadline(self):
        await self.assert_silence("open")

    async def test_auth_silence_has_operation_deadline(self):
        await self.assert_silence("auth")

    async def test_method_silence_has_operation_deadline(self):
        await self.assert_silence("method")

    async def test_auth_and_method_share_one_budget(self):
        for name in self.operations:
            with self.subTest(operation=name):
                peer = NormalOperationPeer(self.dialect, mode="shared_budget")
                async with self.running(name, peer) as task:
                    await self.finished(task)
                    with self.assertRaises(TimeoutError):
                        task.result()
                self.assertTrue(peer.started.is_set(), "must reach method after delayed authentication")

    async def test_completed_normal_operations_preserve_payloads(self):
        for name in self.operations:
            with self.subTest(operation=name):
                peer = NormalOperationPeer(self.dialect, mode="normal")
                async with self.running(name, peer) as task:
                    await self.finished(task)
                    result = task.result()
                methods = [m for m in peer.sent if "method" in m]
                self.assertEqual(len({m["id"] for m in methods}), len(methods))
                self.assertEqual(sum(m["method"] == "auth.login_with_api_key" for m in methods), 1)
                if name == "fetch_all":
                    self.assertEqual(result.enclosures, [{"id": "synthetic-enclosure"}])
                    self.assertEqual(result.disks, [{"name": "da0"}])
                    self.assertEqual(result.pools, [{"name": "synthetic-pool"}])
                    self.assertEqual(result.disk_temperatures, {"da0": 30})
                    self.assertEqual(result.smart_test_results, [{"disk": "da0", "status": "SUCCESS"}])
                    self.assertFalse(result.enclosure_query_failed)
                elif name == "set_slot_status":
                    self.assertIsNone(result)
                    self.assertEqual(methods[-1]["params"], ["synthetic-enclosure", 3, "IDENTIFY"])
                else:
                    self.assertEqual(result, "synthetic SMART")

    async def test_timeout_preserves_primary_despite_late_transport_failure(self):
        for name in self.operations:
            for mode in ("late_send", "late_reader"):
                with self.subTest(operation=name, mode=mode):
                    peer = NormalOperationPeer(self.dialect, mode=mode, close_error=True)
                    async with self.running(name, peer) as task:
                        await asyncio.wait_for(peer.closing.wait(), 0.5)
                        self.assertFalse(task.done())
                        peer.release.set()
                        await self.finished(task)
                        with self.assertRaises(TimeoutError):
                            task.result()
                    self.assertTrue(peer.late_error_raised)

    async def test_repeated_cancellation_drains_late_send_and_reader(self):
        self.client.config.timeout_seconds = 2
        for name in self.operations:
            for mode in ("late_send", "late_reader"):
                with self.subTest(operation=name, mode=mode):
                    peer = NormalOperationPeer(self.dialect, mode=mode, close_error=True)
                    async with self.running(name, peer) as task:
                        await asyncio.wait_for(peer.started.wait(), 0.5)
                        task.cancel()
                        await asyncio.wait_for(peer.closing.wait(), 0.5)
                        for _ in range(2):
                            task.cancel()
                            await asyncio.sleep(0)
                        self.assertFalse(task.done(), "caller must retain teardown ownership")
                        peer.release.set()
                        await self.finished(task)
                        with self.assertRaises(asyncio.CancelledError):
                            task.result()
                    self.assertTrue(peer.late_error_raised)

    async def test_method_and_reader_errors_survive_close_failure(self):
        for name in self.operations:
            for mode in ("method_error", "reader_error"):
                with self.subTest(operation=name, mode=mode):
                    peer = NormalOperationPeer(self.dialect, mode=mode, close_error=True)
                    async with self.running(name, peer) as task:
                        await self.finished(task)
                        if mode == "reader_error":
                            with self.assertRaises(RuntimeError) as caught:
                                task.result()
                            self.assertIs(caught.exception, peer.reader_error)
                        else:
                            with self.assertRaisesRegex(TrueNASAPIError, "synthetic primary rejection"):
                                task.result()

    async def test_cancellation_after_method_error_retains_reader_cleanup(self):
        self.client.config.timeout_seconds = 2
        peer = NormalOperationPeer(self.dialect, mode="error_late_reader", close_error=True)
        async with self.running("fetch_all", peer) as task:
            await asyncio.wait_for(peer.closing.wait(), 0.5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            self.assertFalse(task.done())
            peer.release.set()
            await self.finished(task)
            with self.assertRaises(asyncio.CancelledError):
                task.result()
        self.assertTrue(peer.late_error_raised)

    async def test_reader_failure_while_send_blocked_is_primary(self):
        self.client.config.timeout_seconds = 2
        peer = NormalOperationPeer(self.dialect, mode="reader_during_send", close_error=True)
        async with self.running("fetch_all", peer) as task:
            await asyncio.wait_for(peer.closing.wait(), 0.5)
            peer.release.set()
            await self.finished(task)
            with self.assertRaises(RuntimeError) as caught:
                task.result()
            self.assertIs(caught.exception, peer.reader_error)
        self.assertTrue(peer.late_error_raised)


class NormalOperationDeadlineTests(NormalOperationDeadlineChecks, unittest.IsolatedAsyncioTestCase):
    async def test_handshake_silence_has_operation_deadline(self):
        await self.assert_silence("handshake")

    async def test_application_pings_do_not_reset_deadline(self):
        for name in self.operations:
            for phase in ("auth", "method"):
                for mode in ("ping", "blocked_pong"):
                    with self.subTest(operation=name, phase=phase, mode=mode):
                        peer = NormalOperationPeer(phase=phase, mode=mode)
                        async with self.running(name, peer) as task:
                            await self.finished(task)
                            with self.assertRaises(TimeoutError):
                                task.result()
                        self.assertGreaterEqual(len(peer.pongs), 3 if mode == "ping" else 1)

    async def test_normal_results_wait_for_exact_application_pongs(self):
        for name in self.operations:
            for phase in ("auth", "method"):
                with self.subTest(operation=name, phase=phase):
                    peer = NormalOperationPeer(phase=phase, mode="ping_success")
                    async with self.running(name, peer) as task:
                        await self.finished(task)
                        task.result()
                    self.assertEqual(peer.pongs, [
                        {"msg": "pong"}, {"msg": "pong", "id": ""},
                        {"msg": "pong", "id": "synthetic-ping"},
                    ])


class RealCloseTransport(asyncio.Transport):
    """Installed WebSocket framing/lifecycle over a socket-free silent peer."""

    def __init__(self, connection, dialect, *, rejection=False, auth_rejection=False):
        from websockets.protocol import OPEN, SERVER, Protocol
        self.connection = connection
        self.dialect = dialect
        self.peer = Protocol(SERVER, state=OPEN)
        self.rejection = rejection
        self.auth_rejection = auth_rejection
        self.close_sent = asyncio.Event()
        self.closed = False
        self.close_calls = self.abort_calls = 0
        self.sent = []
        self.deliveries = []

    def set_write_buffer_limits(self, high=None, low=None):
        pass

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass

    def is_closing(self):
        return self.closed

    def can_write_eof(self):
        return False

    def close(self):
        self.close_calls += 1
        self.finish()

    def abort(self):
        self.abort_calls += 1
        self.finish()

    def finish(self):
        if not self.closed:
            self.closed = True
            for handle in self.deliveries:
                handle.cancel()
            self.connection.connection_lost(None)

    def acknowledge_close(self):
        for data in self.peer.data_to_send():
            if data:
                self.connection.data_received(data)
            else:
                # asyncio closes the transport when eof_received returns None.
                self.connection.eof_received()
                if not self.closed:
                    self.close()

    def write(self, data):
        from websockets.frames import Frame, Opcode
        self.peer.receive_data(bytes(data))
        for event in self.peer.events_received():
            assert isinstance(event, Frame)
            if event.opcode == Opcode.CLOSE:
                # Retain the real peer's close acknowledgement until the test
                # releases it. Without release, real close_timeout must abort.
                self.close_sent.set()
                continue
            assert event.opcode == Opcode.TEXT
            request = json.loads(bytes(event.data))
            self.sent.append(request)
            if request.get("msg") == "connect":
                reply = {"msg": "connected"}
            else:
                method = request["method"]
                reply = {"id": request["id"]}
                reply.update({"msg": "result"} if self.dialect == "ddp" else {"jsonrpc": "2.0"})
                if method == "auth.login_with_api_key":
                    reply["result"] = not self.auth_rejection
                elif self.rejection and method in ("pool.query", "enclosure.set_slot_status", "disk.smartctl"):
                    reply["error"] = {"code": -32001, "message": "synthetic close-test rejection"}
                else:
                    reply["result"] = {
                        "enclosure.query": [{"id": "synthetic-enclosure"}],
                        "enclosure2.query": [{"id": "synthetic-enclosure"}],
                        "disk.query": [{"name": "da0"}], "disk.details": {},
                        "pool.query": [], "disk.temperatures": {}, "smart.test.results": [],
                        "enclosure.set_slot_status": None, "disk.smartctl": "synthetic SMART",
                    }[method]
            frame = Frame(Opcode.TEXT, json.dumps(reply).encode()).serialize(mask=False)
            # Leave room between the operation deadline and close deadline so
            # repeated cancellation after expiry occurs during actual close.
            delay = 0.04 if request.get("method") != "auth.login_with_api_key" else 0
            self.deliveries.append(asyncio.get_running_loop().call_later(
                delay, self.connection.data_received, frame,
            ))


class RealWebsocketCloseChecks:
    """Only connect is replaced; handshake, calls and real close run unchanged."""

    dialect = "ddp"
    operations = ("fetch_all", "set_slot_status", "fetch_disk_smartctl")
    async def asyncSetUp(self):
        await NormalOperationDeadlineChecks.asyncSetUp(self)
        blocker = patch("socket.socket.connect_ex", side_effect=AssertionError("unexpected network access"))
        blocker.start()
        self.addCleanup(blocker.stop)

    asyncTearDown = NormalOperationDeadlineChecks.asyncTearDown

    def operation(self, name):
        if name == "smartctl_batch":
            return self.client.smartctl_batch(["da0", "da1"], max_concurrency=2)
        return NormalOperationDeadlineChecks.operation(self, name)

    @asynccontextmanager
    async def real_session(self, name, *, rejection=False, auth_rejection=False, late_error=False):
        from websockets.asyncio.client import ClientConnection, connect as RealConnect
        from websockets.client import ClientProtocol
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        from websockets.protocol import OPEN
        from websockets.uri import parse_uri

        self.client.config.timeout_seconds = 0.16
        connection = ClientConnection(
            ClientProtocol(parse_uri(self.client._endpoint_url()), state=OPEN),
            ping_interval=None, close_timeout=self.client.config.timeout_seconds,
        )
        connection.process_event(Response(101, "Switching Protocols", Headers()))
        transport = RealCloseTransport(connection, self.dialect, rejection=rejection,
                                       auth_rejection=auth_rejection)
        connection.connection_made(transport)
        close_error = RuntimeError("synthetic failure after real close")
        exits = []

        class MemoryConnect:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *args):
                exits.append(asyncio.current_task())
                # Invoke installed context exit, close, send_context and parser.
                self.connection = connection
                await RealConnect.__aexit__(self, *args)
                if late_error:
                    raise close_error

        def connect(url, **kwargs):
            self.assertEqual(url, self.client._endpoint_url())
            self.assertEqual(kwargs["close_timeout"], connection.close_timeout)
            return MemoryConnect()

        task = None
        with patch("app.services.truenas_ws.connect", connect):
            try:
                started = asyncio.get_running_loop().time()
                task = asyncio.create_task(self.operation(name))
                await asyncio.wait_for(transport.close_sent.wait(), 1)
                yield task, connection, transport, started, close_error
                self.assertEqual(len(exits), 1, "do not restart an interrupted close")
                self.assertTrue(exits[0].done())
                methods = [m.get("method") for m in transport.sent]
                self.assertEqual(methods.count("auth.login_with_api_key"), 1)
                if name == "set_slot_status" and not auth_rejection:
                    self.assertEqual(methods.count("enclosure.set_slot_status"), 1, "no LED retries")
            finally:
                # Assertions are made BEFORE this failure-only fixture safety
                # cleanup. It must never be counted as product close completion.
                transport.finish()
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def assert_finished_closed(self, task, connection, transport, *, aborted):
        done, _ = await asyncio.wait({task}, timeout=1)
        self.assertIn(task, done, "public caller did not drain close")
        # No sleep or fixture cleanup between public completion and these checks.
        self.assertTrue(transport.closed, "public caller returned with transport open")
        self.assertTrue(connection.connection_lost_waiter.done(), "connection-lost waiter was abandoned")
        self.assertFalse(connection.connection_lost_waiter.cancelled())
        self.assertEqual(connection.protocol.state.name, "CLOSED")
        self.assertEqual(transport.abort_calls, int(aborted))
        self.assertEqual(transport.close_calls, int(not aborted))

    async def cancelled_close(self, trigger, *, late_error=False, operations=None):
        for name in operations or self.operations:
            for rejection in (False, True):
                with self.subTest(operation=name, rejection=rejection, trigger=trigger):
                    async with self.real_session(name, rejection=rejection, late_error=late_error) as row:
                        task, connection, transport, started, _ = row
                        if trigger.startswith("deadline"):
                            delay = started + self.client.config.timeout_seconds + 0.01
                            await asyncio.sleep(max(0, delay - asyncio.get_running_loop().time()))
                        else:
                            task.cancel()
                            await asyncio.sleep(0)
                        if trigger.endswith("repeated"):
                            for _ in range(3):
                                task.cancel()
                                await asyncio.sleep(0)
                        self.assertFalse(task.done(), "cancellation abandoned already-started close")
                        await self.assert_finished_closed(task, connection, transport, aborted=True)
                        expected = TimeoutError if trigger.startswith("deadline") else asyncio.CancelledError
                        with self.assertRaises(expected):
                            task.result()

    async def test_first_cancel_preserves_installed_close(self):
        await self.cancelled_close("cancel")

    async def test_repeated_cancel_preserves_installed_close_and_primary(self):
        await self.cancelled_close("cancel_repeated", late_error=True)

    async def test_operation_deadline_preserves_installed_close(self):
        await self.cancelled_close("deadline")

    async def test_repeated_cancel_after_deadline_keeps_timeout_primary(self):
        await self.cancelled_close("deadline_repeated", late_error=True)

    async def test_batch_sibling_preserves_installed_close(self):
        await self.cancelled_close("cancel_repeated", late_error=True, operations=("smartctl_batch",))

    async def test_real_close_ack_preserves_results_and_primary_errors(self):
        for name in (*self.operations, "smartctl_batch"):
            for rejection, late_error in ((False, False), (False, True), (True, True)):
                with self.subTest(operation=name, rejection=rejection, late_error=late_error):
                    async with self.real_session(name, rejection=rejection, late_error=late_error) as row:
                        task, connection, transport, _, close_error = row
                        transport.acknowledge_close()
                        await self.assert_finished_closed(task, connection, transport, aborted=False)
                        if rejection:
                            with self.assertRaisesRegex(TrueNASAPIError, "synthetic close-test rejection"):
                                task.result()
                        elif late_error:
                            with self.assertRaises(RuntimeError) as caught:
                                task.result()
                            self.assertIs(caught.exception, close_error)
                        elif name == "fetch_all":
                            self.assertEqual(task.result().disks, [{"name": "da0"}])
                        elif name == "set_slot_status":
                            self.assertIsNone(task.result())
                        else:
                            expected = ["synthetic SMART"] * 2 if name == "smartctl_batch" else "synthetic SMART"
                            self.assertEqual(task.result(), expected)

    async def test_auth_rejection_preserved_after_real_close_and_late_failure(self):
        for name in (*self.operations, "smartctl_batch"):
            with self.subTest(operation=name):
                async with self.real_session(name, auth_rejection=True, late_error=True) as row:
                    task, connection, transport, _, _ = row
                    transport.acknowledge_close()
                    await self.assert_finished_closed(task, connection, transport, aborted=False)
                    with self.assertRaisesRegex(TrueNASAPIError, "authentication failed"):
                        task.result()
                    self.assertEqual([m["method"] for m in transport.sent if "method" in m],
                                     ["auth.login_with_api_key"])

    async def test_uncancelled_library_close_timeout_aborts_transport(self):
        # Batch has no whole-operation deadline during normal session exit.
        # Its successful close is the positive control for the real timeout.
        async with self.real_session("smartctl_batch") as row:
            task, connection, transport, _, _ = row
            await self.assert_finished_closed(task, connection, transport, aborted=True)
            self.assertEqual(task.result(), ["synthetic SMART"] * 2)


class RealWebsocketCloseTests(RealWebsocketCloseChecks, unittest.IsolatedAsyncioTestCase):
    pass


if __name__ == "__main__":
    unittest.main()
