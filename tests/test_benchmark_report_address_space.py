"""The benchmark report records the address-space limit each phase really ran under (#1430).

An owned worker's RLIMIT_AS is the address space it inherited from the forked
supervisor plus a fixed budget. The report must name that budget as a budget
and record, per phase, the inherited size and the limit that was applied. A
phase that did not report them carries null (unknown), never a guess.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import unittest
from unittest.mock import patch

LIMIT_FIELDS = ("address_space_inherited_bytes", "address_space_limit_bytes")


@unittest.skipUnless(sys.platform.startswith("linux"), "the benchmark worker is Linux only")
class BenchmarkReportAddressSpaceTests(unittest.TestCase):
    def setUp(self):
        from scripts import benchmark_full_backup
        self.b = benchmark_full_backup
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The limit the phase actually ran under, written by the forked worker.
        self.observed = tempfile.TemporaryFile()
        self.addCleanup(self.observed.close)

    def record_applied_limit(self, *args):
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        os.write(self.observed.fileno(), f"{soft} {hard}\n".encode("ascii"))
        return {"input_bytes": 0, "output_bytes": 0}

    def read_observed(self) -> list[tuple[int, int]]:
        self.observed.seek(0)
        lines = self.observed.read().decode("ascii").splitlines()
        return [(int(soft), int(hard)) for soft, hard in (line.split() for line in lines)]

    def test_owned_phase_result_records_inherited_size_and_the_applied_limit(self):
        root = self.enterContext(self.b.owned_run(Path(self.tmp.name), self.b.MIB))
        request = {"root": str(root), "target_bytes": self.b.MIB, "timeout": 30}
        with patch.object(self.b, "operation", side_effect=self.record_applied_limit):
            result = self.b.measure_process([], request, root, timeout=30, owned_phase="fixture")
        self.assertEqual(result["state"], "complete", result)
        [(soft, hard)] = self.read_observed()
        self.assertEqual(soft, hard)
        self.assertEqual(result.get("address_space_limit_bytes"), soft)
        inherited = result.get("address_space_inherited_bytes")
        assert isinstance(inherited, int) and not isinstance(inherited, bool), inherited
        self.assertGreater(inherited, 0)
        self.assertEqual(soft, inherited + self.b.GIB)

    def test_report_names_the_budget_and_records_each_phase_limit(self):
        scratch = Path(self.tmp.name) / "scratch"
        scratch.mkdir(mode=0o700)
        with patch.object(self.b, "PHASES", ("fixture", "create")), \
                patch.object(self.b, "operation", side_effect=self.record_applied_limit), \
                patch.object(self.b, "needs_7z", return_value=False), \
                patch.object(self.b.subprocess, "check_output", return_value="0" * 40 + "\n"):
            report = self.b.benchmark(scratch, self.b.MIB, seed=1, timeout=30, archive_format="tar.zst-stream")
        self.assertEqual(report["state"], "complete", report["phases"])
        self.assertEqual(report.get("per_phase_address_space_budget_bytes"), self.b.GIB)
        # The old field stays for existing readers; it is the budget, and the
        # report says so instead of letting it read as the process limit.
        self.assertEqual(report["per_process_address_space_limit_bytes"], self.b.GIB)
        legacy = report.get("per_process_address_space_limit_bytes_meaning", "")
        self.assertIn("per_phase_address_space_budget_bytes", legacy)
        self.assertIn("address_space_limit_bytes", legacy)
        observed = self.read_observed()
        self.assertEqual(len(observed), 2)
        for phase, (soft, _) in zip(report["phases"], observed):
            with self.subTest(phase=phase["phase"]):
                self.assertEqual(phase["address_space_limit_bytes"], soft)
                self.assertEqual(phase["address_space_limit_bytes"],
                                 phase["address_space_inherited_bytes"] + report["per_phase_address_space_budget_bytes"])

    def test_phases_that_did_not_report_carry_unknown_limits(self):
        scratch = Path(self.tmp.name) / "scratch"
        scratch.mkdir(mode=0o700)
        with patch.object(self.b, "PHASES", ("fixture", "create")), \
                patch.object(self.b, "operation", side_effect=RuntimeError("synthetic")), \
                patch.object(self.b, "needs_7z", return_value=False), \
                patch.object(self.b.subprocess, "check_output", return_value="0" * 40 + "\n"):
            report = self.b.benchmark(scratch, self.b.MIB, seed=1, timeout=30, archive_format="tar.zst-stream")
        self.assertEqual([p["state"] for p in report["phases"]], ["failed", "blocked"])
        for phase in report["phases"]:
            with self.subTest(phase=phase["phase"]):
                for key in LIMIT_FIELDS:
                    self.assertIn(key, phase)
                    self.assertIsNone(phase[key])

    def run_fake_worker(self, payload: dict) -> dict:
        scratch = Path(self.tmp.name)
        script = "import sys; sys.stdin.read(); print(sys.argv[1])"
        argv = [sys.executable, "-I", "-c", script, json.dumps(payload)]
        return self.b.measure_process(argv, {"root": str(scratch)}, scratch, timeout=30)

    def test_only_a_consistent_reported_limit_is_carried_into_the_result(self):
        base = {"state": "complete", "wall_seconds": 0, "cpu_seconds": 0, "peak_rss_bytes": 0,
                "input_bytes": 0, "output_bytes": 0}
        good = self.run_fake_worker({**base, "address_space_inherited_bytes": 4096,
                                     "address_space_limit_bytes": 4096 + self.b.GIB})
        self.assertEqual(good["state"], "complete")
        self.assertEqual((good.get("address_space_inherited_bytes"), good.get("address_space_limit_bytes")),
                         (4096, 4096 + self.b.GIB))
        for bad in ({"address_space_inherited_bytes": "4096", "address_space_limit_bytes": 4096 + self.b.GIB},
                    {"address_space_inherited_bytes": True, "address_space_limit_bytes": 1 + self.b.GIB},
                    {"address_space_inherited_bytes": 4096, "address_space_limit_bytes": self.b.GIB},
                    {"address_space_inherited_bytes": -1, "address_space_limit_bytes": self.b.GIB - 1},
                    {"address_space_inherited_bytes": 4096}):
            with self.subTest(payload=bad):
                result = self.run_fake_worker({**base, **bad})
                self.assertEqual(result["state"], "complete")
                self.assertEqual((result.get("address_space_inherited_bytes", "missing"),
                                  result.get("address_space_limit_bytes", "missing")), (None, None))


if __name__ == "__main__":
    unittest.main()
