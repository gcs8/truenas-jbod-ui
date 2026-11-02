"""Loopback fake of the TrueNAS middleware websocket for client limit tests.

It listens on 127.0.0.1 only and never contacts a real appliance. The parts
of the middleware it imitates are the ones issue #1072 depends on, taken from
public TrueNAS sources (CORE ``TN-13.0-U6.8``, SCALE ``release/25.04.0``):

* Every method call runs as a detached task, so a login can finish after the
  client has already closed the socket (CORE ``middlewared/main.py``
  ``Application.on_message``; SCALE ``api/base/server/ws_handler/rpc.py``).
* A successful login registers the session's close callback. Closing the
  socket runs only the callbacks registered so far, so a login that completes
  after close leaves its ``auth.sessions`` entry behind (CORE
  ``plugins/auth.py`` ``SessionManager.login`` / ``_app_on_close``).
* Each connection admits 10 concurrent calls and rejects calls past 20 with
  ``ETOOMANYREFS`` (DDP) or ``-32000`` (JSON-RPC) (``utils/lock.py``
  ``SoftHardSemaphore``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

API_KEY = "synthetic-ephemeral-key"
DDP_BUSY_ERROR = {"error": 59, "type": None, "reason": "Maximum number of concurrent calls (20) has exceeded."}
JSONRPC_BUSY_ERROR = {"code": -32000, "message": "Maximum number of concurrent calls (20) has exceeded"}
RESULTS: dict[str, Any] = {
    "enclosure.query": [],
    "enclosure2.query": [],
    "disk.query": [{"name": "da0"}],
    "disk.details": {"used": [], "unused": []},
    "pool.query": [],
    "disk.temperatures": {"da0": 30},
    "smart.test.results": [],
    "disk.smartctl": "synthetic smartctl output",
    "enclosure.set_slot_status": None,
}


class FakeMiddleware:
    def __init__(self) -> None:
        self.call_delay = 0.0
        # When set, every non-login call answers with the busy error.
        self.busy = False
        # When set, logins answer with the busy error too.
        self.busy_login = False
        # Cleared: logins wait. ``login_delay`` adds a fixed wait afterwards.
        self.login_gate = asyncio.Event()
        self.login_gate.set()
        self.login_delay = 0.0
        # When set, the named method never answers.
        self.hang_methods: set[str] = set()
        # When set, the named method drops the connection without a reply.
        self.drop_methods: set[str] = set()
        self.connections = 0
        self.closes = 0
        self.clean_closes = 0
        self.method_calls: list[str] = []
        self.logins_started = 0
        self.logins_completed = 0
        self.logins_after_close = 0
        self.sessions: dict[str, str] = {}
        self.inflight = 0
        self.peak_inflight = 0
        self.login_started = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._server = None
        self.port = 0

    async def __aenter__(self) -> "FakeMiddleware":
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = list(self._server.sockets)[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.login_gate.set()
        await self.settle()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def settle(self) -> None:
        """Wait for every detached call, including logins finishing after close."""
        while True:
            pending = [task for task in self._tasks if not task.done()]
            if not pending:
                return
            await asyncio.wait(pending)

    async def _handler(self, ws: ServerConnection) -> None:
        jsonrpc = ws.request is not None and ws.request.path.startswith("/api/")
        app: dict[str, Any] = {
            "id": str(uuid.uuid4()), "authenticated": False, "closed": False, "on_close": [], "calls": set(),
        }
        self.connections += 1
        try:
            async for raw in ws:
                message = json.loads(raw)
                if not jsonrpc and message.get("msg") == "connect":
                    await ws.send(json.dumps({"msg": "connected", "session": app["id"]}))
                    continue
                if not jsonrpc and message.get("msg") != "method":
                    continue
                task = asyncio.create_task(self._call(ws, app, message, jsonrpc))
                self._tasks.add(task)
                if message.get("method") != "auth.login_with_api_key":
                    app["calls"].add(task)
        except Exception:
            pass
        finally:
            app["closed"] = True
            # A hung method call is not part of the contract under test; stop
            # it so the server can shut down. Logins keep running on purpose.
            for task in app["calls"]:
                task.cancel()
            for callback in app["on_close"]:
                callback()
            self.closes += 1
            if ws.close_code == 1000:
                self.clean_closes += 1

    async def _reply(self, ws: ServerConnection, message: dict[str, Any], jsonrpc: bool,
                     *, result: Any = None, error: dict[str, Any] | None = None) -> None:
        if jsonrpc:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        else:
            payload = {"msg": "result", "id": message["id"]}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            pass

    async def _call(self, ws: ServerConnection, app: dict[str, Any], message: dict[str, Any], jsonrpc: bool) -> None:
        method = message["method"]
        self.inflight += 1
        self.peak_inflight = max(self.peak_inflight, self.inflight)
        try:
            if method == "auth.login_with_api_key":
                self.logins_started += 1
                self.login_started.set()
                await self.login_gate.wait()
                if self.login_delay:
                    await asyncio.sleep(self.login_delay)
                if self.busy_login:
                    await self._reply(ws, message, jsonrpc, error=JSONRPC_BUSY_ERROR if jsonrpc else DDP_BUSY_ERROR)
                    return
                ok = message["params"] == [API_KEY]
                if ok:
                    self.logins_completed += 1
                    if app["closed"]:
                        self.logins_after_close += 1
                    self.sessions[app["id"]] = "API_KEY"
                    app["authenticated"] = True
                    app["on_close"].append(lambda: self.sessions.pop(app["id"], None))
                await self._reply(ws, message, jsonrpc, result=ok)
                return
            self.method_calls.append(method)
            if not app["authenticated"]:
                await self._reply(ws, message, jsonrpc, error={"error": 13, "reason": "Not authenticated"})
                return
            if self.busy:
                await self._reply(ws, message, jsonrpc, error=JSONRPC_BUSY_ERROR if jsonrpc else DDP_BUSY_ERROR)
                return
            if method in self.drop_methods:
                await ws.close(1011, "synthetic middleware failure")
                return
            if method in self.hang_methods:
                await asyncio.Future()
            if self.call_delay:
                await asyncio.sleep(self.call_delay)
            await self._reply(ws, message, jsonrpc, result=RESULTS.get(method))
        finally:
            self.inflight -= 1
