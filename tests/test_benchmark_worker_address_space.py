"""The owned benchmark worker's address-space budget against a large supervisor (#1325).

An owned worker is a fork of the supervisor, so it starts with the
supervisor's whole address space. These tests grow the supervisor past
1 GiB with a reserved, never-touched mapping (no memory is used) and check
that the phase still gets its own budget, and that the budget still binds.
"""
from __future__ import annotations

import errno
import mmap
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def _address_space_bytes() -> int:
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    raise AssertionError("VmSize missing")


@unittest.skipUnless(sys.platform.startswith("linux"), "the benchmark worker is Linux only")
class BenchmarkWorkerAddressSpaceTests(unittest.TestCase):
    def setUp(self):
        from scripts import benchmark_full_backup
        self.b = benchmark_full_backup
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.enterContext(self.b.owned_run(Path(self.tmp.name), self.b.MIB))
        # Fixed words, exception type and errno names only; written through
        # one descriptor opened before the fork.
        self.notes = tempfile.TemporaryFile()
        self.addCleanup(self.notes.close)

    def note(self, text: str):
        os.write(self.notes.fileno(), text.encode("ascii") + b"\n")

    def note_error(self, step: str, error: BaseException):
        name = errno.errorcode.get(getattr(error, "errno", None) or 0, "")
        self.note(f"{step}-raised {type(error).__name__} {name}".strip())

    def read_notes(self) -> list[str]:
        self.notes.seek(0)
        return self.notes.read().decode("ascii", "replace").splitlines()

    def grow_supervisor_past_one_gib(self):
        # Address space only: a private read-only mapping that is never touched
        # takes no memory and no commit charge.
        size = max(0, self.b.GIB - _address_space_bytes()) + 256 * self.b.MIB
        reserved = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS, prot=mmap.PROT_READ)
        self.addCleanup(reserved.close)
        self.assertGreater(_address_space_bytes(), self.b.GIB)

    def run_owned(self, operation):
        request = {"root": str(self.root), "target_bytes": self.b.MIB, "timeout": 30}
        with patch.object(self.b, "operation", side_effect=operation):
            result = self.b.measure_process([], request, self.root, timeout=30, owned_phase="fixture")
        return result, self.read_notes()

    def test_phase_can_map_memory_and_spawn_under_a_large_supervisor(self):
        self.grow_supervisor_past_one_gib()

        def operation(*args):
            try:
                buffer = bytearray(64 * self.b.MIB)
                del buffer
                self.note("mapped")
                # Hosted Python builds route subprocess.Popen through glibc
                # posix_spawn, which maps a fresh stack for the child.
                pid = os.posix_spawn(sys.executable, [sys.executable, "-c", "pass"], {})
                _, status = os.waitpid(pid, 0)
                self.note(f"spawned {os.waitstatus_to_exitcode(status)}")
            except BaseException as error:
                self.note_error("operation", error)
                raise
            return {"input_bytes": 0, "output_bytes": 0}

        result, notes = self.run_owned(operation)
        self.assertEqual(result["state"], "complete", f"exit_code={result['exit_code']} notes={notes}")
        self.assertEqual(notes, ["mapped", "spawned 0"])

    def test_budget_still_binds_the_phase_under_a_large_supervisor(self):
        self.grow_supervisor_past_one_gib()

        def operation(*args):
            try:
                mmap.mmap(-1, self.b.GIB + 64 * self.b.MIB,
                          flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS, prot=mmap.PROT_READ)
            except BaseException as error:
                self.note_error("operation", error)
                raise
            self.note("over-budget-mapped")
            return {"input_bytes": 0, "output_bytes": 0}

        result, notes = self.run_owned(operation)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(notes, ["operation-raised OSError ENOMEM"])

    def test_budget_admits_and_binds_at_the_runner_size(self):
        def within(*args):
            try:
                mmap.mmap(-1, 512 * self.b.MIB, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS, prot=mmap.PROT_READ).close()
            except BaseException as error:
                self.note_error("operation", error)
                raise
            self.note("within-budget-mapped")
            return {"input_bytes": 0, "output_bytes": 0}

        result, notes = self.run_owned(within)
        self.assertEqual((result["state"], notes), ("complete", ["within-budget-mapped"]))

        self.notes.truncate(0)
        self.notes.seek(0)

        def over(*args):
            try:
                mmap.mmap(-1, self.b.GIB + 64 * self.b.MIB,
                          flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS, prot=mmap.PROT_READ)
            except BaseException as error:
                self.note_error("operation", error)
                raise
            return {"input_bytes": 0, "output_bytes": 0}

        result, notes = self.run_owned(over)
        self.assertEqual((result["state"], notes), ("failed", ["operation-raised OSError ENOMEM"]))

    def test_unreadable_address_space_size_refuses_before_any_limit_or_operation(self):
        import io
        import json
        request = {"root": str(self.root), "target_bytes": self.b.MIB, "timeout": 5}
        with patch("sys.stdin", io.StringIO(json.dumps(request))), \
                patch.object(self.b, "address_space_bytes", side_effect=OSError(errno.ENOENT, "unreadable")), \
                patch.object(self.b.resource, "setrlimit") as setrlimit, \
                patch.object(self.b, "operation", return_value={}) as operation:
            self.assertEqual(self.b.worker("fixture"), 1)
        setrlimit.assert_not_called()
        operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
