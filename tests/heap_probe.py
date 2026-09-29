"""Peak-heap probes that a coverage tracer cannot slow down or distort.

`tracemalloc` records a traceback for every allocation while it is running.
`coverage run` allocates per-frame bookkeeping for every line it traces, so
each of those tracer allocations is itself captured by `tracemalloc`: the two
costs multiply instead of adding.

On CI run 34815729312 the 16 MiB streaming-JSON preflight probe in
tests/test_system_backup.py took 4.2s on the uninstrumented Python 3.14 job and
365.2s on the Python 3.12 coverage job - 68% of that job's 535.2s test body,
and the single largest reason the protected matrix could reach its 20-minute
limit (#513). A standalone reproduction of the mechanism over an 8 MiB
per-byte parse measured 1.55s uninstrumented, 1.98s under `tracemalloc` alone,
5.72s under `coverage` alone, and 38.33s under both, with the traced peak
growing from 48 to 3806 bytes.

That last number is the second reason for this module: under a tracer the
value a probe asserts is not the product's heap, it is the product's heap plus
the tracer's.

`start()` therefore detaches the active trace function before it starts
`tracemalloc`, and `stop()` reattaches it. Only frames entered while a probe is
running go untraced; coverage resumes on the next frame the caller enters. The
product lines a probe exercises stay covered by the neighbouring functional
tests that call the same code without a probe, so aggregate coverage is
unchanged. Do not call `tracemalloc.start()` directly in a test -
tests/test_heap_probe.py fails the suite if you do.
"""

from __future__ import annotations

import sys
import tracemalloc
from types import FrameType
from typing import Any, Callable, Optional

TraceFunction = Callable[[FrameType, str, Any], Any]

_suspended_trace: list[Optional[TraceFunction]] = []


def start(nframe: int = 1) -> None:
    """Detach the active trace function, then start `tracemalloc`."""

    if _suspended_trace:
        raise RuntimeError("a heap probe is already running")
    _suspended_trace.append(sys.gettrace())
    sys.settrace(None)
    caller = sys._getframe(1)
    caller.f_trace = None
    caller.f_trace_lines = False
    tracemalloc.start(nframe)


def get_traced_memory() -> tuple[int, int]:
    """Return `(current, peak)` for the running probe."""

    if not _suspended_trace:
        raise RuntimeError("no heap probe is running")
    return tracemalloc.get_traced_memory()


def stop() -> None:
    """Stop `tracemalloc`, then reattach the suspended trace function."""

    try:
        tracemalloc.stop()
    finally:
        if _suspended_trace:
            sys.settrace(_suspended_trace.pop())


def is_tracing() -> bool:
    """Report whether a probe is currently measuring."""

    return tracemalloc.is_tracing()
