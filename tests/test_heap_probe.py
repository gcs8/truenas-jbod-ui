"""Contract for the untraced peak-heap probe helper.

The deterministic suite measures peak heap with `tracemalloc`. `tracemalloc`
records a traceback for every allocation while it runs, and `coverage run`
allocates per-frame bookkeeping for every line it traces, so the two costs
multiply. On CI run 34815729312 the 16 MiB streaming-JSON preflight probe took
4.2s on the uninstrumented Python 3.14 job and 365.2s on the Python 3.12
coverage job: 68% of that job's 535s test body and the reason the protected
matrix could reach its 20-minute limit (#513).

These tests pin the fix so the multiplication cannot come back: probes go
through `tests.heap_probe`, which detaches the active trace function while it
measures.
"""

from __future__ import annotations

import re
import sys
import tracemalloc
import unittest
from pathlib import Path

from tests import heap_probe

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
DIRECT_TRACEMALLOC_CALL = re.compile(r"\btracemalloc\.(start|get_traced_memory)\s*\(")


class UntracedHeapProbeTests(unittest.TestCase):
    def tearDown(self) -> None:
        if tracemalloc.is_tracing():  # pragma: no cover - probe leak guard
            tracemalloc.stop()

    def test_probe_detaches_the_active_trace_function_while_measuring(self) -> None:
        events: list[str] = []

        def tracer(frame, event, arg):  # noqa: ANN001, ANN202 - trace protocol
            events.append(frame.f_code.co_name)
            return tracer

        def allocate() -> list[bytes]:
            return [b"x" * 1024 for _ in range(16)]

        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            heap_probe.start()
            events.clear()
            try:
                held = allocate()
                _, peak_bytes = heap_probe.get_traced_memory()
            finally:
                heap_probe.stop()
            traced_during_probe = list(events)
            restored = sys.gettrace()
        finally:
            sys.settrace(previous)

        self.assertEqual(len(held), 16)
        self.assertGreater(peak_bytes, 0)
        self.assertEqual(
            traced_during_probe,
            [],
            "the trace function still ran during the heap probe",
        )
        self.assertIs(restored, tracer, "the trace function was not restored")

    def test_probe_restores_the_trace_function_when_the_body_raises(self) -> None:
        def tracer(frame, event, arg):  # noqa: ANN001, ANN202 - trace protocol
            return tracer

        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            heap_probe.start()
            with self.assertRaises(RuntimeError):
                try:
                    raise RuntimeError("probe body failed")
                finally:
                    heap_probe.stop()
            restored = sys.gettrace()
        finally:
            sys.settrace(previous)

        self.assertIs(restored, tracer)
        self.assertFalse(tracemalloc.is_tracing())

    def test_nested_probes_are_rejected_instead_of_losing_the_trace_function(self) -> None:
        heap_probe.start()
        try:
            with self.assertRaises(RuntimeError):
                heap_probe.start()
        finally:
            heap_probe.stop()

    def test_no_test_module_starts_tracemalloc_outside_the_helper(self) -> None:
        offenders = sorted(
            path.name
            for path in TESTS_DIR.glob("*.py")
            if path.name != "heap_probe.py"
            and DIRECT_TRACEMALLOC_CALL.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            offenders,
            [],
            "peak-heap probes must go through tests.heap_probe so the coverage "
            "tracer is detached while they measure (#513)",
        )


if __name__ == "__main__":  # pragma: no cover - manual entry point
    unittest.main()
