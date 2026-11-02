from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import ssl
import time
import uuid
import weakref
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.config import TRUENAS_API_VERSION_PATTERN, TrueNASConfig, effective_max_concurrent_calls
from app.services.tls_context import build_tls_client_context, resolve_tls_server_name

logger = logging.getLogger(__name__)

# Inventory layouts and operator-configured enclosure profiles support at most
# 4096 slots. Accepting more API disk rows cannot represent a supported layout
# and would let appliance-controlled cardinality drive model allocation.
MAX_DISK_INVENTORY_ROWS = 4096
_DISK_IDENTITY_KEYS = (
    "name",
    "devname",
    "device",
    "disk",
    "identifier",
    "serial",
    "serial_lunid",
    "lunid",
    "multipath_name",
    "multipath_member",
    "zfs_guid",
)


class TrueNASAPIError(RuntimeError):
    retryable = False

    def __init__(
        self,
        *args: Any,
        errname: str | None = None,
        reason: str | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(*args)
        self.errname = errname
        self.reason = reason
        self.code = code


class TrueNASAPIBusyError(TrueNASAPIError):
    """The middleware refused the call for capacity reasons; the same call may succeed later."""

    retryable = True


class _LimiterBusyError(TrueNASAPIBusyError):
    """Raised by the app's own backoff, before anything is sent."""


# Standard JSON-RPC missing-method code and TrueNAS middleware custom codes.
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_TOO_MANY_CONCURRENT_CALLS = -32000
JSONRPC_METHOD_CALL_ERROR = -32001


MethodCaller = Callable[[str, list[Any]], Awaitable[Any]]

# The DDP endpoint reports a full call queue as ETOOMANYREFS (59 on CORE's
# FreeBSD, 109 on SCALE's Linux) with this reason text.
_DDP_BUSY_REASON = "maximum number of concurrent calls"
_DDP_BUSY_ERRNOS = frozenset({59, 109})


def build_ddp_error(method: str, error: Any) -> TrueNASAPIError:
    busy = False
    if isinstance(error, dict):
        reason = str(error.get("reason") or "").lower()
        busy = _DDP_BUSY_REASON in reason or (not reason and error.get("error") in _DDP_BUSY_ERRNOS)
    error_class = TrueNASAPIBusyError if busy else TrueNASAPIError
    return error_class(f"{method} failed: {error}")


class MiddlewareCallLimiter:
    """One appliance's budget of concurrent middleware calls, plus busy backoff (#1072).

    Every call the app makes to one appliance - inventory reads, SMART grid
    batches, slot drawers, LED actions and logins - takes a slot here, so the
    total stays at ``limit`` however many requests overlap. A busy reply, or a
    whole inventory read that times out, starts a cooldown during which calls
    fail at once with `TrueNASAPIBusyError` instead of reaching the appliance.
    The cooldown doubles on each further busy reply, up to
    ``max_delay_seconds``, and is jittered down by at most half so separate
    workers do not retry in step.
    """

    base_delay_seconds = 2.0
    max_delay_seconds = 60.0

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._failures = 0
        self._cooldown_until = 0.0
        self.clock: Callable[[], float] = time.monotonic
        self.random: Callable[[], float] = random.random

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self.clock())

    def check_open(self) -> None:
        remaining = self.cooldown_remaining()
        if remaining > 0:
            raise _LimiterBusyError(
                f"TrueNAS middleware is busy; backing off for {max(1, round(remaining))}s "
                "before calling it again."
            )

    def note_busy(self) -> None:
        self._failures += 1
        delay = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (self._failures - 1))
        delay *= 0.5 + 0.5 * self.random()
        self._cooldown_until = max(self._cooldown_until, self.clock() + delay)

    def note_success(self) -> None:
        # A call admitted before a busy reply can still succeed; it must not
        # cut the cooldown short.
        if self.cooldown_remaining() == 0:
            self._failures = 0

    async def _acquire(self) -> None:
        if self.active < self.limit and not self._waiters:
            self.active += 1
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                # The slot was handed over just as the caller was cancelled.
                self._release()
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            raise

    def set_limit(self, limit: int) -> None:
        """The newest configuration wins; queued calls are admitted if it grew."""
        self.limit = limit
        self._admit()

    def _release(self) -> None:
        self.active -= 1
        self._admit()

    def _admit(self) -> None:
        while self._waiters and self.active < self.limit:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self.active += 1
                waiter.set_result(None)

    @asynccontextmanager
    async def slot(self):
        self.check_open()
        await self._acquire()
        try:
            # A busy reply may have arrived while this call was queued.
            self.check_open()
            yield
        except _LimiterBusyError:
            raise
        except TrueNASAPIBusyError:
            # Record it before releasing, so the next queued call sees it.
            self.note_busy()
            raise
        else:
            self.note_success()
        finally:
            self._release()


# One limiter per appliance and event loop. Keyed by the normalized endpoint so
# every client for the same appliance (including the one a config reload
# builds) shares it; asyncio primitives cannot cross event loops.
_LIMITERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, MiddlewareCallLimiter]] = (
    weakref.WeakKeyDictionary()
)


def middleware_call_limiter(config: TrueNASConfig) -> MiddlewareCallLimiter:
    loop = asyncio.get_running_loop()
    per_loop = _LIMITERS.setdefault(loop, {})
    key = build_websocket_url(config.host)
    limit = effective_max_concurrent_calls(config)
    limiter = per_loop.get(key)
    if limiter is None:
        limiter = per_loop[key] = MiddlewareCallLimiter(limit)
    elif limiter.limit != limit:
        limiter.set_limit(limit)
    return limiter


def build_jsonrpc_error(method: str, error: Any) -> TrueNASAPIError:
    """Map a JSON-RPC error object onto the app's API error.

    `data.trace` is middleware internals and is deliberately never carried into
    the message the UI shows; `errname` and `reason` are what an operator acts on.
    """
    code: int | None = None
    errname = reason = message_text = ""
    if isinstance(error, dict):
        raw_code = error.get("code")
        if isinstance(raw_code, int) and not isinstance(raw_code, bool):
            code = raw_code
        message_text = str(error.get("message") or "").strip()
        data = error.get("data")
        if isinstance(data, dict):
            errname = str(data.get("errname") or "").strip()
            reason = str(data.get("reason") or "").strip()
    if code == JSONRPC_METHOD_NOT_FOUND and not errname:
        errname = "ENOMETHOD"
        reason = reason or message_text
    detail = ": ".join(part for part in (errname, reason) if part) or message_text
    if not detail:
        detail = f"code {code}" if code is not None else "unspecified middleware error"
    error_class = TrueNASAPIBusyError if code == JSONRPC_TOO_MANY_CONCURRENT_CALLS else TrueNASAPIError
    return error_class(
        f"{method} failed: {detail}",
        errname=errname or None,
        reason=reason or None,
        code=code,
    )


def normalize_disk_inventory_rows(value: Any, *, source: str = "disk inventory") -> list[dict[str, Any]]:
    if isinstance(value, list):
        if len(value) > MAX_DISK_INVENTORY_ROWS:
            raise TrueNASAPIError(
                f"{source} returned {len(value)} rows; the supported maximum is {MAX_DISK_INVENTORY_ROWS}."
            )
        rows = value
    elif isinstance(value, dict):
        rows = [value]
    else:
        return []

    return [
        row
        for row in rows
        if isinstance(row, dict)
        and any(
            not isinstance(value := row.get(key), bool)
            and isinstance(value, (str, int))
            and bool(str(value).strip())
            for key in _DISK_IDENTITY_KEYS
        )
    ]


class _MiddlewareCallDispatcher:
    def __init__(self, ws: ClientConnection, *, limiter: MiddlewareCallLimiter | None = None) -> None:
        self.ws = ws
        self._limiter = limiter
        self._pending: dict[Any, tuple[str, asyncio.Future[Any]]] = {}
        self._reader_task = asyncio.create_task(self._reader())

    def _next_request_id(self) -> Any:
        return str(uuid.uuid4())

    def _build_payload(self, request_id: Any, method: str, params: list[Any]) -> dict[str, Any]:
        return {
            "id": request_id,
            "msg": "method",
            "method": method,
            "params": params,
        }

    async def call(
        self, method: str, params: list[Any], *, timeout: Callable[[], float] | None = None,
    ) -> Any:
        """Send one call once the appliance's limiter admits it.

        ``timeout`` is read after admission, so time spent queued behind other
        calls to the same appliance does not count against this call's reply.
        """
        if self._limiter is None:
            return await self._call_with_timeout(method, params, timeout)
        async with self._limiter.slot():
            return await self._call_with_timeout(method, params, timeout)

    async def _call_with_timeout(
        self, method: str, params: list[Any], timeout: Callable[[], float] | None,
    ) -> Any:
        if timeout is None:
            return await self._call(method, params)
        async with asyncio.timeout(timeout()):
            return await self._call(method, params)

    async def _call(self, method: str, params: list[Any]) -> Any:
        request_id = self._next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = (method, future)
        payload = self._build_payload(request_id, method, params)
        logger.debug("Calling TrueNAS websocket method %s", method)
        try:
            await self.ws.send(json.dumps(payload))
            return await future
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A reader failure can settle this future while send() is still
                # blocked. Observe it even if cancellation prevents awaiting it.
                future.exception()

    async def close(self) -> None:
        if not self._reader_task.done():
            self._reader_task.cancel()
        try:
            await self._reader_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Reader failures are delivered to every pending call. Re-raising the
            # same failure during cleanup can mask the call failure or caller
            # cancellation that initiated shutdown.
            pass

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if message.get("msg") == "ping":
            # DDP heartbeats are JSON messages, not WebSocket control frames.
            pong = {"msg": "pong"}
            if "id" in message:
                pong["id"] = message["id"]
            await self.ws.send(json.dumps(pong))
            return
        if message.get("msg") != "result":
            return
        request_id = message.get("id")
        if not isinstance(request_id, str):
            return
        pending = self._pending.get(request_id)
        if pending is None:
            return
        method, future = pending
        if future.done():
            return
        if message.get("error"):
            future.set_exception(build_ddp_error(method, message["error"]))
        else:
            future.set_result(message.get("result"))

    async def _reader(self) -> None:
        try:
            while True:
                raw_message = await self.ws.recv()
                message = json.loads(raw_message)
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for _method, future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
            raise


class _JsonRpcCallDispatcher(_MiddlewareCallDispatcher):
    """JSON-RPC 2.0 calls over one connection.

    Reuses the pending-call and reader lifecycle above; only the request shape,
    the id space (integers, unique per connection) and the response mapping
    differ. Keepalive is the WebSocket ping/pong the client library answers, not
    a DDP heartbeat message.
    """

    def __init__(
        self, ws: ClientConnection, *, request_ids: Iterator[int] | None = None,
        limiter: MiddlewareCallLimiter | None = None,
    ) -> None:
        self._request_ids = request_ids if request_ids is not None else itertools.count(1)
        super().__init__(ws, limiter=limiter)

    def _next_request_id(self) -> int:
        return next(self._request_ids)

    def _build_payload(self, request_id: Any, method: str, params: list[Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict) or "id" not in message:
            # Notifications carry no id and answer no call.
            return
        request_id = message.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            return
        pending = self._pending.get(request_id)
        if pending is None:
            return
        method, future = pending
        if future.done():
            return
        error = message.get("error")
        if error is not None:
            future.set_exception(build_jsonrpc_error(method, error))
        else:
            future.set_result(message.get("result"))


def build_websocket_url(host: str) -> str:
    if "://" not in host:
        host = f"https://{host}"

    parsed = urlsplit(host)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/")
    path = f"{path}/websocket" if path else "/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def build_jsonrpc_url(host: str, api_version: str = "current") -> str:
    if not TRUENAS_API_VERSION_PATTERN.fullmatch(api_version or ""):
        raise TrueNASAPIError(
            f"Unsupported TrueNAS api_version {api_version!r}; expected 'current' or a pinned 'vX.Y.Z'."
        )

    if "://" not in host:
        host = f"https://{host}"

    parsed = urlsplit(host)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, f"{path}/api/{api_version}", "", ""))


@dataclass(slots=True)
class TrueNASRawData:
    enclosures: list[dict[str, Any]]
    disks: list[dict[str, Any]]
    pools: list[dict[str, Any]]
    disk_temperatures: dict[str, int]
    smart_test_results: list[dict[str, Any]]
    enclosure_query_failed: bool = False
    systems: list[dict[str, Any]] = field(default_factory=list)
    pool_devices: list[dict[str, Any]] = field(default_factory=list)
    ha_groups: list[dict[str, Any]] = field(default_factory=list)
    hw_disks: list[dict[str, Any]] = field(default_factory=list)
    hw_enclosures: list[dict[str, Any]] = field(default_factory=list)
    cli_disks: list[dict[str, Any]] = field(default_factory=list)
    cli_hw_disks: list[dict[str, Any]] = field(default_factory=list)
    cli_hw_enclosures: list[dict[str, Any]] = field(default_factory=list)
    cli_network_ports: list[dict[str, Any]] = field(default_factory=list)


# #1072: a connection that closes while its login is still running leaves an
# API session behind on the appliance, so close waits for the login's reply
# for up to one call timeout, capped here.
LOGIN_SETTLE_MAX_SECONDS = 15.0


@dataclass(slots=True)
class _PendingLogin:
    request_id: Any = None
    # A send interrupted mid-frame leaves the connection unusable.
    sending: bool = False
    holds_slot: bool = False
    limiter: MiddlewareCallLimiter | None = None

    def release(self) -> None:
        if self.holds_slot and self.limiter is not None:
            self.holds_slot = False
            self.limiter._release()


# #537: how much longer than one call the whole phase may take before the
# remaining positions are reported instead of started.
SMART_BATCH_PHASE_DEADLINE_MULTIPLIER = 2
# A full inventory read is five middleware calls, and CORE's
# `smart.test.results` alone can take several times one call's timeout on a
# large shelf. LED and single-disk SMART calls keep the plain timeout.
INVENTORY_DEADLINE_MULTIPLIER = 4


class TrueNASWebsocketClient:
    """
    Minimal websocket client for TrueNAS middleware calls.

    Two wire dialects sit behind one call surface, chosen per host by
    `config.api_dialect`:

    * `ddp` (default) speaks the legacy `/websocket` endpoint that TrueNAS CORE
      and every SCALE release expose.
    * `jsonrpc` speaks the JSON-RPC 2.0 API at `/api/<api_version>` that SCALE
      25.04 and newer document as supported; CORE has no such endpoint.

    Both expose `auth.login_with_api_key` and method calls. We keep the call
    surface intentionally small: only the methods needed by this app live here.
    """

    def __init__(self, config: TrueNASConfig) -> None:
        self.config = config
        # JSON-RPC ids must be unique per connection. One counter per client
        # keeps the handshake call and the dispatcher from colliding.
        self._jsonrpc_request_ids = itertools.count(1)

    @property
    def call_limiter(self) -> MiddlewareCallLimiter:
        """The limiter every client for this appliance shares (#1072)."""
        return middleware_call_limiter(self.config)

    @property
    def _uses_jsonrpc(self) -> bool:
        return self.config.api_dialect == "jsonrpc"

    def _endpoint_url(self) -> str:
        if self._uses_jsonrpc:
            return build_jsonrpc_url(self.config.host, self.config.api_version)
        return build_websocket_url(self.config.host)

    def _make_dispatcher(self, ws: ClientConnection) -> _MiddlewareCallDispatcher:
        if self._uses_jsonrpc:
            return _JsonRpcCallDispatcher(ws, request_ids=self._jsonrpc_request_ids, limiter=self.call_limiter)
        return _MiddlewareCallDispatcher(ws, limiter=self.call_limiter)

    async def _run_operation(self, operation: Awaitable[Any], *, multiplier: int = 1) -> Any:
        """One monotonic budget for connect, authentication and all replies.

        Transport ping_timeout only measures WebSocket keepalive. Neither those
        frames nor application pings extend this operation's lifetime. Keep a
        separate owner so repeated caller cancellation cannot interrupt drainage,
        and a late send/close error cannot replace the timeout or cancellation.
        """
        owner = asyncio.ensure_future(operation)
        budget = self.config.timeout_seconds * multiplier
        try:
            done, _ = await asyncio.wait({owner}, timeout=budget)
            if not done:
                raise TimeoutError(f"TrueNAS operation did not complete within {budget:g}s.")
            return owner.result()
        except BaseException:
            owner.cancel()
            await self._drain_owned(owner)
            raise

    @staticmethod
    async def _drain_owned(owner: asyncio.Future[Any]) -> asyncio.CancelledError | None:
        cancelled = None
        while not owner.done():
            try:
                # wait() neither forwards cancellation nor abandons shield
                # futures which can report unobserved late failures.
                await asyncio.wait({owner})
            except asyncio.CancelledError as exc:
                cancelled = exc
        if not owner.cancelled():
            owner.exception()
        return cancelled

    async def fetch_all(self) -> TrueNASRawData:
        try:
            return await self._run_operation(self._fetch_all(), multiplier=INVENTORY_DEADLINE_MULTIPLIER)
        except TimeoutError:
            # A whole inventory read outlasting its budget means the middleware
            # is not keeping up; back off instead of starting the next one at
            # once. A single slow disk (#523) does not trigger this.
            self.call_limiter.note_busy()
            raise

    async def _fetch_all(self) -> TrueNASRawData:
        async with self._session() as ws:
            dispatcher = self._make_dispatcher(ws)

            async def fetch_enclosures() -> tuple[list[dict[str, Any]], bool]:
                try:
                    return await self._fetch_enclosures(dispatcher.call), False
                except TrueNASAPIError:
                    logger.warning(
                        "TrueNAS enclosure methods failed; retaining the other API payloads with degraded topology."
                    )
                    return [], True

            fetch_tasks = [
                asyncio.create_task(fetch_enclosures()),
                asyncio.create_task(self._fetch_disks(dispatcher.call)),
                asyncio.create_task(self._fetch_pools(dispatcher.call)),
                asyncio.create_task(self._fetch_disk_temperatures(dispatcher.call)),
                asyncio.create_task(self._fetch_smart_test_results(dispatcher.call)),
            ]
            gathered = asyncio.gather(*fetch_tasks)
            primary: BaseException | None = None
            try:
                # A reader failure must also interrupt calls blocked in send().
                await asyncio.wait({gathered, dispatcher._reader_task}, return_when=asyncio.FIRST_COMPLETED)
                if dispatcher._reader_task.done():
                    try:
                        dispatcher._reader_task.result()
                    except ConnectionClosed:
                        # A peer may close after delivering every reply. Only
                        # completed consumers can decide the operation outcome;
                        # incomplete calls (including blocked sends) still fail.
                        if not all(task.done() for task in fetch_tasks):
                            raise
                enclosure_result, disks, pools, disk_temperatures, smart_test_results = await gathered
            except BaseException as exc:
                primary = exc
                raise
            finally:
                async def cleanup() -> None:
                    # gather() leaves siblings running after its first failure.
                    for task in fetch_tasks:
                        task.cancel()
                    await asyncio.gather(*fetch_tasks, gathered, return_exceptions=True)
                    await dispatcher.close()

                # Retain teardown even if cancellation arrives after an ordinary
                # failure has already started closing the reader.
                closing = asyncio.create_task(cleanup())
                cancelled = await self._drain_owned(closing)
                if cancelled is not None:
                    raise cancelled
                if primary is None:
                    closing.result()
            enclosures, enclosure_query_failed = enclosure_result
            return TrueNASRawData(
                enclosures=self._ensure_list(enclosures),
                disks=self._ensure_list(disks),
                pools=self._ensure_list(pools),
                disk_temperatures=disk_temperatures,
                smart_test_results=smart_test_results,
                enclosure_query_failed=enclosure_query_failed,
            )

    async def fetch_disk_smartctl(self, disk_name: str, args: list[str] | None = None) -> str:
        return await self._run_operation(self._fetch_disk_smartctl(disk_name, args))

    async def _fetch_disk_smartctl(self, disk_name: str, args: list[str] | None = None) -> str:
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

    async def smartctl_batch(
        self, disks: list[str], args: list[str] | None = None, *, max_concurrency: int,
        return_exceptions: bool = False,
    ) -> list[str] | list[str | BaseException]:
        """Fetch a bounded, positional SMART batch over one authenticated session.

        Fail fast without partial results or retries. The budget is request-local;
        duplicate disk names remain separate calls. Cancellation drains owned work
        before returning, including when the caller cancels more than once.

        With ``return_exceptions``, a failure that belongs to ONE disk - a
        middleware rejection for a device it cannot open or a name not in its
        disk table (#522), or a reply slower than the per-call deadline (#523) -
        is returned at that disk's position instead of discarding the whole
        batch. A failure of the session itself still raises: no result in it is
        trustworthy, and there is nothing to keep.
        """
        if type(max_concurrency) is not int or max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer.")
        if not isinstance(disks, (list, tuple)):
            raise ValueError("disks must be a finite list or tuple of disk names.")
        if len(disks) > MAX_DISK_INVENTORY_ROWS:
            raise TrueNASAPIError(f"SMART batch exceeds the supported maximum of {MAX_DISK_INVENTORY_ROWS} disks.")
        if any(not isinstance(disk, str) or not disk for disk in disks):
            raise ValueError("disks must contain nonempty string disk names.")
        if not disks:
            return []
        # Snapshot caller-owned containers before any await, without deduplication.
        # The appliance's call limit caps the batch width too (#1072), so a
        # grid cannot queue more workers than the limiter will ever admit.
        width = min(max_concurrency, len(disks), self.call_limiter.limit)
        owner = asyncio.create_task(self._run_smartctl_batch(
            list(disks), list(args or ["-a", "-j"]), width,
            return_exceptions=bool(return_exceptions),
        ))
        try:
            await asyncio.wait({owner})
        except asyncio.CancelledError:
            owner.cancel()
            # wait() doesn't propagate repeated cancellation into the cleanup
            # owner (nor leave abandoned shield futures with late exceptions).
            while not owner.done():
                try:
                    await asyncio.wait({owner})
                except asyncio.CancelledError:
                    pass
            if not owner.cancelled():
                owner.exception()
            raise
        return owner.result()

    async def _run_smartctl_batch(
        self, disks: list[str], args: list[str], width: int, *, return_exceptions: bool = False,
    ) -> list[str] | list[str | BaseException]:
        session = self._session()
        async with asyncio.timeout(self.config.timeout_seconds):
            ws = await session.__aenter__()
        primary: BaseException | None = None
        dispatcher = self._make_dispatcher(ws)
        positions = iter(enumerate(disks))
        results: list[Any] = [""] * len(disks)
        # #537: the per-call deadline is per disk, so a shelf where every disk
        # stalls costs one timeout per round: 84 disks at width 12 held the
        # batch open for about seven of them. The phase carries its own
        # deadline, measured from the last disk that actually answered, so a
        # healthy long batch keeps going while a stalled one stops after a
        # bounded wait and reports the positions it never reached; the caller
        # falls back for those instead of waiting.
        phase_seconds = self.config.timeout_seconds * SMART_BATCH_PHASE_DEADLINE_MULTIPLIER
        loop = asyncio.get_running_loop()
        phase_deadline = loop.time() + phase_seconds

        def note_progress() -> None:
            nonlocal phase_deadline
            phase_deadline = max(phase_deadline, loop.time() + phase_seconds)

        async def worker() -> None:
            for position, disk in positions:
                remaining = phase_deadline - loop.time()
                if remaining <= 0:
                    expired = TimeoutError(
                        f"disk.smartctl for {disk!r} was not started within the "
                        f"{phase_seconds:g}s batch deadline."
                    )
                    if not return_exceptions:
                        raise expired
                    results[position] = expired
                    continue
                try:
                    result = await dispatcher.call(
                        "disk.smartctl", [disk, args],
                        timeout=lambda: min(self.config.timeout_seconds, max(phase_deadline - loop.time(), 0.001)),
                    )
                except TrueNASAPIError as exc:
                    if self.config.platform == "scale" and "ENOMETHOD" in str(exc):
                        # Not about this disk: the method is absent, so nothing
                        # in this batch can succeed. Fail the whole batch.
                        raise TrueNASAPIError(
                            "Detailed SMART JSON is not available through the SCALE websocket API on this system."
                        ) from exc
                    if not return_exceptions:
                        raise
                    results[position] = exc
                    continue
                except TimeoutError as exc:
                    if not return_exceptions:
                        raise
                    # A reply slower than the per-call deadline belongs to this
                    # disk, not to the session: the dispatcher has already
                    # dropped the request id, so a late reply is discarded
                    # rather than settling another position, and the remaining
                    # workers keep draining the queue (#523). Reported as a
                    # named TimeoutError so the caller's log says which disk.
                    timeout_error = TimeoutError(
                        f"disk.smartctl for {disk!r} did not answer within "
                        f"{self.config.timeout_seconds:g}s."
                    )
                    timeout_error.__cause__ = exc
                    results[position] = timeout_error
                    continue
                if not isinstance(result, str):
                    payload_error = TrueNASAPIError(
                        f"disk.smartctl returned unexpected payload type for {disk!r}."
                    )
                    if not return_exceptions:
                        raise payload_error
                    results[position] = payload_error
                    continue
                results[position] = result
                note_progress()

        tasks = [asyncio.create_task(worker()) for _ in range(width)]
        gathered = asyncio.gather(*tasks)
        try:
            # A reader can fail while a worker is still inside send(). Wake the
            # batch immediately rather than waiting for that worker's deadline.
            await asyncio.wait({gathered, dispatcher._reader_task}, return_when=asyncio.FIRST_COMPLETED)
            if dispatcher._reader_task.done():
                try:
                    dispatcher._reader_task.result()
                except ConnectionClosed:
                    # Completed positions retain their results or application
                    # error; transport loss cannot complete unfinished workers.
                    if not all(task.done() for task in tasks):
                        raise
            await gathered
        except BaseException as exc:
            primary = exc
            raise
        finally:
            async def cleanup() -> None:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, gathered, return_exceptions=True)
                try:
                    await dispatcher.close()
                finally:
                    await session.__aexit__(
                        type(primary) if primary else None, primary,
                        primary.__traceback__ if primary else None,
                    )

            # Cancellation may arrive after a method already failed and cleanup
            # started. Keep reader/session teardown owned in that race too.
            closing = asyncio.create_task(cleanup())
            cancelled: asyncio.CancelledError | None = None
            while not closing.done():
                try:
                    await asyncio.wait({closing})
                except asyncio.CancelledError as exc:
                    cancelled = exc
            try:
                closing.result()
            except BaseException:
                if primary is None and cancelled is None:
                    raise
            if cancelled is not None:
                raise cancelled
        return results

    async def set_slot_status(self, enclosure_id: str, slot_number: int, status: str) -> None:
        await self._run_operation(self._set_slot_status(enclosure_id, slot_number, status))

    async def _set_slot_status(self, enclosure_id: str, slot_number: int, status: str) -> None:
        async with self._session() as ws:
            await self._call(ws, "enclosure.set_slot_status", [enclosure_id, slot_number, status])

    def _ssl_context(self) -> ssl.SSLContext | None:
        websocket_url = self._endpoint_url()
        if not websocket_url.startswith("wss://"):
            return None
        return build_tls_client_context(self.config)

    async def _perform_handshake(self, ws: ClientConnection, login: _PendingLogin) -> None:
        if not self._uses_jsonrpc:
            await ws.send(json.dumps({"msg": "connect", "version": "1", "support": ["1"]}))
            response = json.loads(await ws.recv())
            if response.get("msg") != "connected":
                raise TrueNASAPIError(f"Unexpected websocket handshake response: {response}")
        # JSON-RPC 2.0 has no session handshake; authentication is the first call.
        if await self._login(ws, login) is not True:
            raise TrueNASAPIError("TrueNAS API authentication failed.")

    async def _login(self, ws: ClientConnection, login: _PendingLogin) -> Any:
        """Authenticate, remembering a login that is still in flight.

        The middleware runs every call, the login included, as a detached task
        and registers the session's close-time logout only when the login
        completes. If the socket closes first, the login still finishes and
        its session is never removed (#1072). `_session` therefore waits for
        a pending login's reply, within a bounded grace, before it closes.
        """
        limiter = self.call_limiter
        limiter.check_open()
        await limiter._acquire()
        login.holds_slot = True
        login.limiter = limiter
        method = "auth.login_with_api_key"
        request_id, payload = self._request_payload(method, [self.config.api_key])
        logger.debug("Calling TrueNAS middleware method %s", method)
        login.sending = True
        await ws.send(json.dumps(payload))
        login.sending = False
        # Only a login the appliance has received can complete after close.
        login.request_id = request_id
        result = await self._await_reply(ws, method, request_id, login=login)
        login.request_id = None
        login.release()
        return result

    async def _settle_login(
        self, ws: ClientConnection, login: _PendingLogin, primary: BaseException | None,
    ) -> None:
        """Read until an interrupted login answers, for at most one call timeout.

        Only a timeout or cancellation leaves a healthy connection with a login
        still running. Any other failure, or a frame interrupted mid-send,
        means the connection cannot be read safely, so it closes at once.
        """
        try:
            if (
                login.request_id is not None
                and not login.sending
                and isinstance(primary, (TimeoutError, asyncio.CancelledError))
            ):
                async with asyncio.timeout(min(float(self.config.timeout_seconds), LOGIN_SETTLE_MAX_SECONDS)):
                    await self._await_reply(ws, "auth.login_with_api_key", login.request_id)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            # The login's outcome no longer matters; only that it finished.
            if isinstance(exc, TimeoutError):
                logger.warning(
                    "TrueNAS login did not answer within the close grace; the appliance "
                    "may keep a stale API session until its middleware restarts."
                )
        finally:
            login.request_id = None
            login.release()

    async def _fetch_enclosures(self, call_method: MethodCaller) -> list[dict[str, Any]]:
        methods = ["enclosure.query", "enclosure2.query"]
        if self.config.platform == "scale":
            methods = ["enclosure2.query", "enclosure.query"]

        first_error: TrueNASAPIError | None = None
        for method in methods:
            try:
                result = await call_method(method, [])
            except TrueNASAPIError as exc:
                if first_error is None:
                    first_error = exc
                continue
            return self._ensure_list(result)

        assert first_error is not None
        raise first_error

    async def _fetch_disks(self, call_method: MethodCaller) -> list[dict[str, Any]]:
        try:
            query_payload = await call_method("disk.query", [[], {"extra": {"pools": True}}])
        except TrueNASAPIError:
            logger.warning("disk.query with extra.pools failed; retrying without extra options.")
            query_payload = await call_method("disk.query", [[]])
        query_disks = normalize_disk_inventory_rows(query_payload, source="disk.query")

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
                    if len(combined) + len(rows) > MAX_DISK_INVENTORY_ROWS:
                        raise TrueNASAPIError(
                            "disk.details returned more than "
                            f"{MAX_DISK_INVENTORY_ROWS} rows; the supported maximum is "
                            f"{MAX_DISK_INVENTORY_ROWS}."
                        )
                    combined.extend(rows)
            payload = combined
        return normalize_disk_inventory_rows(payload, source="disk.details")

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
        websocket_url = self._endpoint_url()
        ssl_context = self._ssl_context()

        if not self.config.api_key:
            raise TrueNASAPIError("TRUENAS_API_KEY is required for API access.")
        # Fail fast while backing off: no connection, no login.
        self.call_limiter.check_open()

        connection = connect(
            websocket_url,
            ssl=ssl_context,
            server_hostname=resolve_tls_server_name(self.config) if ssl_context else None,
            open_timeout=self.config.timeout_seconds,
            close_timeout=self.config.timeout_seconds,
            ping_interval=20,
            ping_timeout=self.config.timeout_seconds,
        )
        ws = await connection.__aenter__()
        primary: BaseException | None = None
        login = _PendingLogin()
        try:
            await self._perform_handshake(ws, login)
            yield ws
        except BaseException as exc:
            primary = exc
            raise
        finally:
            # Own context exit separately BEFORE awaiting it. The first cancel
            # can arrive after a result/error has already started close; letting
            # it reach close() would cancel the library's timeout/abort path.
            async def finish() -> None:
                try:
                    await self._settle_login(ws, login, primary)
                finally:
                    await connection.__aexit__(
                        type(primary) if primary is not None else None, primary,
                        primary.__traceback__ if primary is not None else None,
                    )

            closing = asyncio.create_task(finish())
            cancelled = await self._drain_owned(closing)
            if cancelled is not None:
                raise cancelled
            # Drain observes late close failures without replacing the original
            # authentication, method or cancellation error. Successful calls
            # still report an otherwise unmasked close failure.
            if primary is None:
                closing.result()

    async def _call(self, ws: ClientConnection, method: str, params: list[Any]) -> Any:
        async with self.call_limiter.slot():
            if self._uses_jsonrpc:
                return await self._call_jsonrpc(ws, method, params)
            return await self._call_ddp(ws, method, params)

    def _request_payload(self, method: str, params: list[Any]) -> tuple[Any, dict[str, Any]]:
        if self._uses_jsonrpc:
            request_id: Any = next(self._jsonrpc_request_ids)
            return request_id, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        request_id = str(uuid.uuid4())
        return request_id, {"id": request_id, "msg": "method", "method": method, "params": params}

    async def _await_reply(
        self, ws: ClientConnection, method: str, request_id: Any, *, login: _PendingLogin | None = None,
    ) -> Any:
        if self._uses_jsonrpc:
            return await self._await_jsonrpc_reply(ws, method, request_id)
        return await self._await_ddp_reply(ws, method, request_id, login=login)

    async def _call_jsonrpc(self, ws: ClientConnection, method: str, params: list[Any]) -> Any:
        request_id, payload = self._request_payload(method, params)
        logger.debug("Calling TrueNAS JSON-RPC method %s", method)
        await ws.send(json.dumps(payload))
        return await self._await_jsonrpc_reply(ws, method, request_id)

    async def _await_jsonrpc_reply(self, ws: ClientConnection, method: str, request_id: Any) -> Any:
        while True:
            message = json.loads(await ws.recv())
            if not isinstance(message, dict):
                continue
            response_id = message.get("id")
            # Notifications carry no id; a reply for another id answers another call.
            if not isinstance(response_id, int) or isinstance(response_id, bool):
                continue
            if response_id != request_id:
                continue
            error = message.get("error")
            if error is not None:
                raise build_jsonrpc_error(method, error)
            return message.get("result")

    async def _call_ddp(self, ws: ClientConnection, method: str, params: list[Any]) -> Any:
        request_id, payload = self._request_payload(method, params)
        logger.debug("Calling TrueNAS websocket method %s", method)
        await ws.send(json.dumps(payload))
        return await self._await_ddp_reply(ws, method, request_id)

    async def _await_ddp_reply(
        self, ws: ClientConnection, method: str, request_id: Any, *, login: _PendingLogin | None = None,
    ) -> Any:
        while True:
            raw_message = await ws.recv()
            message = json.loads(raw_message)
            msg_type = message.get("msg")

            if msg_type == "ping":
                pong = {"msg": "pong"}
                if "id" in message:
                    pong["id"] = message["id"]
                if login is not None:
                    login.sending = True
                await ws.send(json.dumps(pong))
                if login is not None:
                    login.sending = False
                continue

            if message.get("id") != request_id:
                continue

            if msg_type == "result":
                if "error" in message:
                    raise build_ddp_error(method, message["error"])
                return message.get("result")

            raise TrueNASAPIError(f"Unexpected response for {method}: {message}")

    @staticmethod
    def _ensure_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []
