"""Upper bound for test waits on work that is certain to happen (#1177).

A test that waits for a worker thread, an executor hop or a scheduled task to
reach a point must not fail because a loaded host scheduled that work late.
Such waits use this bound, which only fires on a real hang. Keep a short bound
only where the elapsed time is itself what the test asserts.
"""

from __future__ import annotations

import asyncio

HANG_TIMEOUT_SECONDS = 30.0


async def wait_until(condition, *, timeout: float = HANG_TIMEOUT_SECONDS) -> None:
    """Poll ``condition`` on the running loop until true; fail only at ``timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.01)
