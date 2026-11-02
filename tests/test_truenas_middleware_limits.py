"""#1072: per-appliance middleware call limit, busy backoff and session cleanup.

Every test talks to the loopback fake in ``tests/fake_truenas_middleware.py``
through the real websocket client; nothing contacts a real appliance.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig, load_settings
from app.services.truenas_ws import TrueNASAPIBusyError, TrueNASAPIError, TrueNASWebsocketClient
from tests.fake_truenas_middleware import API_KEY, FakeMiddleware


def make_client(fake: FakeMiddleware, *, limit: int | None = 2, platform: str = "core",
                dialect: str = "ddp", api_key: str = API_KEY, timeout: float = 5) -> TrueNASWebsocketClient:
    client = TrueNASWebsocketClient(TrueNASConfig(
        host=fake.host, api_key=api_key, platform=platform, api_dialect=dialect,
        max_concurrent_calls=limit,
    ))
    # The model field is a whole number of seconds; tests need shorter budgets.
    client.config.timeout_seconds = timeout
    return client


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def wait_for_closes(fake: FakeMiddleware, deadline: float = 3.0) -> None:
    """The server notices a close after the client returns; wait for it."""
    await fake.settle()
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while fake.closes < fake.connections and loop.time() < end:
        await asyncio.sleep(0.01)
    await fake.settle()


class ConcurrencyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_holds_across_every_call_path_and_client(self) -> None:
        async with FakeMiddleware() as fake:
            fake.call_delay = 0.02
            refresh = make_client(fake, limit=2)
            # A config reload builds a second client for the same appliance;
            # both must draw from the same budget.
            drawer = make_client(fake, limit=2)
            disks = [f"da{index}" for index in range(12)]
            await asyncio.wait_for(asyncio.gather(
                refresh.fetch_all(),
                refresh.smartctl_batch(disks, max_concurrency=8),
                *(drawer.fetch_disk_smartctl(disk) for disk in disks[:4]),
                drawer.set_slot_status("enclosure0", 1, "IDENTIFY"),
            ), 20)
            await wait_for_closes(fake)

        self.assertGreater(fake.connections, 1)
        self.assertLessEqual(fake.peak_inflight, 2)
        self.assertEqual(fake.sessions, {})

    async def test_smart_batch_width_is_capped_by_the_appliance_limit(self) -> None:
        async with FakeMiddleware() as fake:
            fake.call_delay = 0.02
            client = make_client(fake, limit=3)
            disks = [f"da{index}" for index in range(9)]
            results = await asyncio.wait_for(client.smartctl_batch(disks, max_concurrency=12), 10)
            await wait_for_closes(fake)

        self.assertEqual(results, ["synthetic smartctl output"] * 9)
        self.assertLessEqual(fake.peak_inflight, 3)

    async def test_jsonrpc_dialect_uses_the_same_limit(self) -> None:
        async with FakeMiddleware() as fake:
            fake.call_delay = 0.02
            client = make_client(fake, limit=2, platform="scale", dialect="jsonrpc")
            await asyncio.wait_for(asyncio.gather(
                client.fetch_all(),
                client.smartctl_batch([f"sd{index}" for index in range(6)], max_concurrency=6),
            ), 10)
            await wait_for_closes(fake)

        self.assertLessEqual(fake.peak_inflight, 2)


class TimedOutCallPermitTests(unittest.IsolatedAsyncioTestCase):
    """A call the client stopped waiting for is still running on the appliance."""

    async def test_timed_out_batch_calls_keep_their_permits(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake, limit=2, timeout=0.1)
            fake.hang_methods = {"disk.smartctl"}
            results = await asyncio.wait_for(
                client.smartctl_batch([f"da{index}" for index in range(6)], max_concurrency=2,
                                      return_exceptions=True), 10,
            )
            await wait_for_closes(fake)
        self.assertTrue(all(isinstance(item, TimeoutError) for item in results), results)
        # Replacement calls must not pile up behind unanswered ones.
        self.assertLessEqual(fake.peak_inflight, 2)
        self.assertEqual(client.call_limiter.active, 0)

    async def test_timed_out_drawer_call_keeps_its_permit_until_close(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake, limit=1, timeout=0.1)
            fake.hang_methods = {"disk.smartctl"}
            with self.assertRaises(TimeoutError):
                await client.fetch_disk_smartctl("da0")
            await wait_for_closes(fake)
        self.assertEqual(client.call_limiter.active, 0)
        self.assertEqual(fake.closes, fake.connections)


class BusyBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_login_backs_off(self) -> None:
        for dialect, platform in (("ddp", "core"), ("jsonrpc", "scale")):
            with self.subTest(dialect=dialect):
                async with FakeMiddleware() as fake:
                    client = make_client(fake, dialect=dialect, platform=platform)
                    client.call_limiter.clock = FakeClock()
                    fake.busy_login = True
                    for _ in range(5):
                        with self.assertRaises(TrueNASAPIBusyError):
                            await client.fetch_disk_smartctl("da0")
                    await wait_for_closes(fake)
                self.assertEqual((fake.connections, fake.logins_started), (1, 1))
                self.assertGreater(client.call_limiter.cooldown_remaining(), 0)
                self.assertEqual(client.call_limiter.active, 0)

    async def test_repeated_calls_to_a_busy_appliance_do_not_storm_it(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake)
            fake.busy = True
            outcomes = await asyncio.gather(
                *(client.fetch_disk_smartctl(f"da{index}") for index in range(10)),
                return_exceptions=True,
            )
            for _ in range(10):
                with self.assertRaises(TrueNASAPIError):
                    await client.fetch_all()
            await wait_for_closes(fake)
        self.assertTrue(all(isinstance(item, TrueNASAPIError) for item in outcomes), outcomes)
        # Calls already admitted when the first busy reply arrives may still
        # be answered; nothing new is sent after it.
        self.assertLessEqual(len(fake.method_calls), 2)
        self.assertEqual(fake.sessions, {})

    async def test_busy_reply_backs_off_instead_of_retrying(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake)
            clock = FakeClock()
            client.call_limiter.clock = clock
            client.call_limiter.random = lambda: 1.0
            fake.busy = True
            errors = []
            for _ in range(10):
                try:
                    await asyncio.wait_for(client.fetch_disk_smartctl("da0"), 5)
                except TrueNASAPIError as exc:
                    errors.append(exc)
            await wait_for_closes(fake)
            self.assertEqual(len(errors), 10)
            self.assertTrue(all(isinstance(exc, TrueNASAPIBusyError) for exc in errors), errors)
            # Only the first attempt reached the appliance.
            self.assertEqual((fake.connections, fake.method_calls), (1, ["disk.smartctl"]))
            self.assertIn("busy", str(errors[-1]).lower())
            first_delay = client.call_limiter.cooldown_remaining()
            self.assertGreater(first_delay, 0)

            # After the cooldown one attempt goes through; a second busy reply
            # doubles the cooldown, and the doubling stops at the cap.
            clock.now += first_delay + 0.01
            with self.assertRaises(TrueNASAPIBusyError):
                await client.fetch_disk_smartctl("da0")
            self.assertEqual(fake.method_calls, ["disk.smartctl"] * 2)
            self.assertAlmostEqual(client.call_limiter.cooldown_remaining(), first_delay * 2, places=3)
            for _ in range(12):
                clock.now += client.call_limiter.cooldown_remaining() + 0.01
                with self.assertRaises(TrueNASAPIBusyError):
                    await client.fetch_disk_smartctl("da0")
            self.assertLessEqual(client.call_limiter.cooldown_remaining(), client.call_limiter.max_delay_seconds)

            # A healthy answer clears the backoff.
            fake.busy = False
            clock.now += client.call_limiter.cooldown_remaining() + 0.01
            self.assertEqual(await client.fetch_disk_smartctl("da0"), "synthetic smartctl output")
            self.assertEqual(client.call_limiter.cooldown_remaining(), 0)
            await wait_for_closes(fake)
        self.assertEqual(fake.sessions, {})

    async def test_jitter_shortens_the_delay_within_bounds(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake)
            client.call_limiter.clock = FakeClock()
            client.call_limiter.random = lambda: 0.0
            fake.busy = True
            with self.assertRaises(TrueNASAPIBusyError):
                await client.fetch_disk_smartctl("da0")
            await wait_for_closes(fake)
        limiter = client.call_limiter
        self.assertAlmostEqual(limiter.cooldown_remaining(), limiter.base_delay_seconds * 0.5, places=3)

    async def test_busy_reply_inside_a_smart_batch_stops_new_starts(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake, limit=2)
            client.call_limiter.clock = FakeClock()
            fake.busy = True
            results = await asyncio.wait_for(
                client.smartctl_batch([f"da{index}" for index in range(20)], max_concurrency=2,
                                      return_exceptions=True), 10,
            )
            await wait_for_closes(fake)
        self.assertTrue(all(isinstance(item, TrueNASAPIBusyError) for item in results), results)
        self.assertLessEqual(len(fake.method_calls), 2)

    async def test_jsonrpc_busy_code_backs_off(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake, platform="scale", dialect="jsonrpc")
            client.call_limiter.clock = FakeClock()
            fake.busy = True
            for _ in range(5):
                with self.assertRaises(TrueNASAPIBusyError):
                    await client.fetch_disk_smartctl("sda")
            await wait_for_closes(fake)
        self.assertEqual((fake.connections, len(fake.method_calls)), (1, 1))

    async def test_refresh_timeout_backs_off(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake, timeout=0.1)
            client.call_limiter.clock = FakeClock()
            fake.hang_methods = {"pool.query"}
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(client.fetch_all(), 5)
            for _ in range(5):
                with self.assertRaises(TrueNASAPIBusyError):
                    await client.fetch_all()
            await wait_for_closes(fake)
        self.assertEqual(fake.connections, 1)
        self.assertEqual(fake.sessions, {})

    async def test_single_disk_timeout_does_not_back_off(self) -> None:
        # #523: a disk that never answers is that disk's fault, not the appliance's.
        async with FakeMiddleware() as fake:
            client = make_client(fake, timeout=0.1)
            client.call_limiter.clock = FakeClock()
            fake.hang_methods = {"disk.smartctl"}
            with self.assertRaises(TimeoutError):
                await client.fetch_disk_smartctl("da0")
            fake.hang_methods = set()
            self.assertEqual(await client.fetch_disk_smartctl("da1"), "synthetic smartctl output")
            await wait_for_closes(fake)
        self.assertEqual(client.call_limiter.cooldown_remaining(), 0)


class SessionCloseTests(unittest.IsolatedAsyncioTestCase):
    async def run_and_audit(self, configure, operation, expected) -> FakeMiddleware:
        async with FakeMiddleware() as fake:
            client = configure(fake)
            if expected is None:
                await asyncio.wait_for(operation(client), 10)
            else:
                with self.assertRaises(expected):
                    await asyncio.wait_for(operation(client), 10)
            await wait_for_closes(fake)
            self.assertEqual(fake.closes, fake.connections)
            self.assertEqual(fake.sessions, {})
            self.assertEqual(fake.logins_after_close, 0)
        return fake

    async def test_opens_equal_closes_on_every_exit_path(self) -> None:
        def plain(fake):
            return make_client(fake)

        def wrong_key(fake):
            return make_client(fake, api_key="synthetic-wrong-key")

        def dropping(fake):
            fake.drop_methods = {"pool.query", "disk.smartctl"}
            return make_client(fake)

        def hanging(fake):
            fake.hang_methods = {"pool.query", "disk.smartctl"}
            return make_client(fake, timeout=0.1)

        def busy(fake):
            fake.busy = True
            return make_client(fake)

        cases = (
            ("refresh success", plain, lambda c: c.fetch_all(), None),
            ("batch success", plain, lambda c: c.smartctl_batch(["da0", "da1"], max_concurrency=2), None),
            ("led success", plain, lambda c: c.set_slot_status("enclosure0", 1, "CLEAR"), None),
            ("auth failure", wrong_key, lambda c: c.fetch_all(), TrueNASAPIError),
            ("dispatcher error", dropping, lambda c: c.fetch_all(), Exception),
            ("batch dispatcher error", dropping, lambda c: c.smartctl_batch(["da0"], max_concurrency=1), Exception),
            ("refresh timeout", hanging, lambda c: c.fetch_all(), TimeoutError),
            ("single timeout", hanging, lambda c: c.fetch_disk_smartctl("da0"), TimeoutError),
            ("busy", busy, lambda c: c.fetch_disk_smartctl("da0"), TrueNASAPIBusyError),
        )
        for name, configure, operation, expected in cases:
            with self.subTest(name):
                await self.run_and_audit(configure, operation, expected)

    async def test_cancellation_closes_every_session(self) -> None:
        async with FakeMiddleware() as fake:
            fake.hang_methods = {"pool.query", "disk.smartctl"}
            client = make_client(fake)
            tasks = [
                asyncio.create_task(client.fetch_all()),
                asyncio.create_task(client.fetch_disk_smartctl("da0")),
                asyncio.create_task(client.smartctl_batch(["da0", "da1"], max_concurrency=2)),
            ]
            while len(fake.method_calls) < 3 or "pool.query" not in fake.method_calls:
                await asyncio.sleep(0.01)
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertTrue(all(isinstance(item, asyncio.CancelledError) for item in results), results)
            await wait_for_closes(fake)
            self.assertEqual(fake.closes, fake.connections)
            self.assertEqual(fake.sessions, {})


class LoginSettleTests(unittest.IsolatedAsyncioTestCase):
    """The CORE 13 leak: a login that completes after close keeps its session.

    The middleware runs the login as a detached task and registers the
    session's close callback only when that login completes, so a client that
    closes while the login is still running leaves an ``auth.sessions`` entry
    behind for good.
    """

    async def _late_login(self, operation, *, cancel: bool = False, dialect: str = "ddp",
                          platform: str = "core", release_after: float = 0.7) -> FakeMiddleware:
        async with FakeMiddleware() as fake:
            client = make_client(fake, timeout=0.5, dialect=dialect, platform=platform)
            fake.login_gate.clear()
            task = asyncio.create_task(operation(client))
            await asyncio.wait_for(fake.login_started.wait(), 5)
            if cancel:
                await asyncio.sleep(0.05)
                task.cancel()
            # Release the login after the client gave up on it, but inside the
            # bounded close grace (one call timeout).
            await asyncio.sleep(release_after)
            fake.login_gate.set()
            with self.assertRaises((TimeoutError, asyncio.CancelledError)):
                await asyncio.wait_for(task, 10)
            await wait_for_closes(fake)
        self.assertEqual(fake.logins_completed, 1)
        self.assertEqual(fake.logins_after_close, 0)
        self.assertEqual(fake.sessions, {})
        self.assertEqual(fake.closes, fake.connections)
        return fake

    async def test_timeout_during_login_waits_for_the_login_before_closing(self) -> None:
        await self._late_login(lambda c: c.fetch_disk_smartctl("da0"))

    async def test_refresh_timeout_during_login(self) -> None:
        async def short_refresh(client):
            client.config.timeout_seconds = 0.25  # 4x multiplier: 1 s budget, 0.25 s grace
            return await client.fetch_all()

        fake = await self._late_login(short_refresh, release_after=1.1)
        self.assertEqual(fake.method_calls, [])

    async def test_smart_batch_timeout_during_login(self) -> None:
        await self._late_login(lambda c: c.smartctl_batch(["da0"], max_concurrency=1))

    async def test_cancellation_during_login(self) -> None:
        # Cancelled at 0.05 s; the grace runs to about 0.55 s.
        await self._late_login(lambda c: c.fetch_disk_smartctl("da0"), cancel=True, release_after=0.25)

    async def test_jsonrpc_timeout_during_login(self) -> None:
        await self._late_login(lambda c: c.fetch_disk_smartctl("sda"), dialect="jsonrpc", platform="scale")

    async def test_settle_wait_is_bounded(self) -> None:
        async with FakeMiddleware() as fake:
            client = make_client(fake, timeout=0.2)
            fake.login_gate.clear()
            loop = asyncio.get_running_loop()
            started = loop.time()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(client.fetch_disk_smartctl("da0"), 5)
            elapsed = loop.time() - started
            fake.login_gate.set()
            await wait_for_closes(fake)
        # Budget plus at most one more timeout of grace, never an open-ended wait.
        self.assertLess(elapsed, 0.2 * 2 + 0.5)


class MaxConcurrentCallsConfigTests(unittest.TestCase):
    def test_platform_defaults_stay_under_the_appliance_soft_limit(self) -> None:
        from app.config import effective_max_concurrent_calls

        self.assertEqual(effective_max_concurrent_calls(TrueNASConfig(platform="core")), 4)
        self.assertEqual(effective_max_concurrent_calls(TrueNASConfig(platform="scale")), 8)
        self.assertEqual(effective_max_concurrent_calls(TrueNASConfig(platform="core", max_concurrent_calls=7)), 7)

    def test_bounds_are_validated(self) -> None:
        for value in (0, -1, 11, True, 2.5, "many"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TrueNASConfig(max_concurrent_calls=value)
        for value in (1, 10):
            self.assertEqual(TrueNASConfig(max_concurrent_calls=value).max_concurrent_calls, value)

    def test_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write("truenas:\n  platform: core\n")
            with patch.dict(os.environ, {"APP_CONFIG_PATH": config_path, "TRUENAS_MAX_CONCURRENT_CALLS": "3"}):
                settings = load_settings()
        self.assertEqual(settings.truenas.max_concurrent_calls, 3)


class InventoryBusyStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_appliance_is_reported_as_busy_not_unreachable(self) -> None:
        from tests.test_inventory import build_inventory_service

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        system = SystemConfig(
            id="synthetic-busy",
            truenas=TrueNASConfig(platform="core", host="https://truenas.example.test"),
            ssh=SSHConfig(enabled=False),
        )
        api = AsyncMock()
        api.fetch_all.side_effect = TrueNASAPIBusyError(
            "TrueNAS middleware is busy; backing off for 5s before the next call."
        )
        service = build_inventory_service(Settings(), system, api, AsyncMock(), directory.name)
        bundle = await service._collect_inventory_source_bundle()

        self.assertFalse(bundle.sources["api"].ok)
        self.assertIn("busy", bundle.sources["api"].message.lower())
        self.assertFalse(any("unreachable" in warning for warning in bundle.warnings), bundle.warnings)
        self.assertTrue(any("busy" in warning.lower() for warning in bundle.warnings), bundle.warnings)


if __name__ == "__main__":
    unittest.main()
