from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_perf_harness.py"
SPEC = importlib.util.spec_from_file_location("run_perf_harness", MODULE_PATH)
assert SPEC and SPEC.loader
run_perf_harness = importlib.util.module_from_spec(SPEC)
sys.modules["run_perf_harness"] = run_perf_harness
SPEC.loader.exec_module(run_perf_harness)


class PerfHarnessTests(unittest.TestCase):
    def test_parse_server_timing_extracts_named_stage_durations(self) -> None:
        header = 'app;desc="total";dur=101.4, stage-1;desc="inventory.build_snapshot";dur=87.2, stage-2;desc="smart.ssh.fetch x4";dur=42.0'

        metrics = run_perf_harness.parse_server_timing(header)

        self.assertEqual(metrics["total"], 101.4)
        self.assertEqual(metrics["inventory.build_snapshot"], 87.2)
        self.assertEqual(metrics["smart.ssh.fetch x4"], 42.0)

    def test_compare_summary_reports_avg_and_p95_deltas(self) -> None:
        current = [
            {"name": "inventory_force", "avg_ms": 120.0, "p95_ms": 180.0},
            {"name": "inventory_cached", "avg_ms": 20.0, "p95_ms": 30.0},
        ]
        baseline = [
            {"name": "inventory_force", "avg_ms": 100.0, "p95_ms": 150.0},
            {"name": "inventory_cached", "avg_ms": 25.0, "p95_ms": 35.0},
        ]

        comparison = run_perf_harness.compare_summary(current, baseline)

        self.assertEqual(comparison[0]["avg_delta_ms"], 20.0)
        self.assertEqual(comparison[0]["p95_delta_ms"], 30.0)
        self.assertEqual(comparison[1]["avg_delta_ms"], -5.0)
        self.assertEqual(comparison[1]["p95_delta_pct"], -14.3)

    def test_summarize_includes_stage_rollups_and_request_count(self) -> None:
        results = [
            run_perf_harness.RunResult(
                name="smart_batch",
                duration_ms=120.0,
                request_count=1,
                stage_totals_ms={"smart.batch.total": 110.0, "smart.ssh.fetch": 90.0},
            ),
            run_perf_harness.RunResult(
                name="smart_batch",
                duration_ms=100.0,
                request_count=1,
                stage_totals_ms={"smart.batch.total": 95.0, "smart.ssh.fetch": 70.0},
            ),
        ]

        summary = run_perf_harness.summarize(results)

        self.assertEqual(summary[0]["avg_requests"], 1.0)
        self.assertEqual(summary[0]["stage_summary"][0]["label"], "smart.batch.total")
        self.assertEqual(summary[0]["stage_summary"][0]["avg_ms"], 102.5)

    def test_write_history_files_creates_latest_and_csv_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record_dir = Path(temp_dir)
            payload = {
                "run_id": "1234567890",
                "recorded_at": "2026-04-17T18:30:00Z",
                "label": "baseline",
                "base_url": "http://127.0.0.1:8080",
                "system_id": None,
                "enclosure_id": None,
                "smart_batch_max_concurrency": 12,
                "smart_prefetch_chunk_size": 24,
                "smart_prefetch_batch_concurrency": 2,
                "git": {"branch": "v0.9.0", "commit": "abc1234", "dirty": True},
                "summary": [
                    {
                        "name": "inventory_force",
                        "iterations": 3,
                        "avg_requests": 1.0,
                        "min_ms": 90.0,
                        "avg_ms": 100.0,
                        "p50_ms": 100.0,
                        "p95_ms": 110.0,
                        "max_ms": 115.0,
                        "stage_summary": [],
                    }
                ],
                "comparison": [],
                "baseline": None,
            }

            artifacts = run_perf_harness.write_history_files(record_dir, payload)

            latest_json = Path(artifacts["latest_json"])
            history_csv = Path(artifacts["history_csv"])
            self.assertTrue(latest_json.exists())
            self.assertTrue(history_csv.exists())
            latest_payload = json.loads(latest_json.read_text(encoding="utf-8"))
            self.assertEqual(latest_payload["label"], "baseline")
            csv_text = history_csv.read_text(encoding="utf-8")
            self.assertIn("inventory_force", csv_text)
            self.assertIn("baseline", csv_text)
