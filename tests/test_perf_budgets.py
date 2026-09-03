from __future__ import annotations

import asyncio
import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app.config import Settings
from app.main import templates
from app.services.snapshot_export import (
    EXPORT_HISTORY_CACHE,
    EXPORT_RENDER_CACHE,
    EXPORT_ZIP_CACHE,
    SnapshotExportService,
)
from history_service.store import HistoryStore

from tests.perf_fixtures import (
    FIXTURE_VERSION,
    FIXTURE_GENERATED_AT,
    HISTORY_METRIC_NAMES,
    MODELED_SLOT_COUNTS,
    MODELED_THRESHOLDS,
    build_modeled_inventory_snapshot,
    build_modeled_scope_history,
)
from scripts import build_perf_baseline
from scripts.build_perf_baseline import build_baseline, compare_baselines


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_OR_SECRET_PATTERN = re.compile(
    r"(?:\b(?:10|127)\.\d+\.\d+\.\d+\b|\b192\.168\.\d+\.\d+\b|"
    r"\b172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+\b|"
    r"(?i:\b(?:api[_-]?key|password|secret|token)\b\s*[:=]))"
)


def compact_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ModeledPerfFixtureTests(unittest.TestCase):
    @staticmethod
    def _comparison_payload() -> dict[str, object]:
        return {
            "schema_version": 3,
            "fixture_version": 1,
            "modeled": True,
            "wall_clock_policy": "report-only",
            "comparison_policy": {
                "exact_metrics": ["slot_count", "query_count"],
                "byte_metrics": ["payload_bytes"],
                "byte_drift_percent": 10,
                "minimum_byte_drift": 4096,
                "byte_drift_direction": "symmetric",
            },
            "cases": {
                "60": {
                    "slot_count": 60,
                    "query_count": 1,
                    "payload_bytes": 50_000,
                    "thresholds": {"payload_bytes": 75_000},
                }
            },
        }

    def test_baseline_comparison_allows_bounded_byte_drift(self) -> None:
        baseline = self._comparison_payload()
        measured = copy.deepcopy(baseline)
        measured["cases"]["60"]["payload_bytes"] = 54_096  # type: ignore[index]

        self.assertEqual(compare_baselines(baseline, measured), [])

    def test_baseline_comparison_rejects_byte_drift_beyond_policy(self) -> None:
        baseline = self._comparison_payload()
        measured = copy.deepcopy(baseline)
        measured["cases"]["60"]["payload_bytes"] = 55_001  # type: ignore[index]

        errors = compare_baselines(baseline, measured)

        self.assertTrue(any("payload_bytes drifted" in error for error in errors), errors)

    def test_baseline_comparison_rejects_hard_ceiling_even_within_drift(self) -> None:
        baseline = self._comparison_payload()
        baseline["cases"]["60"]["thresholds"]["payload_bytes"] = 50_100  # type: ignore[index]
        measured = copy.deepcopy(baseline)
        measured["cases"]["60"]["payload_bytes"] = 50_101  # type: ignore[index]

        errors = compare_baselines(baseline, measured)

        self.assertTrue(any("hard ceiling" in error for error in errors), errors)

    def test_generated_baseline_rejects_inlined_static_assets_over_hard_ceiling(self) -> None:
        baseline = build_baseline()
        measured = copy.deepcopy(baseline)

        for slot_count in MODELED_SLOT_COUNTS:
            with self.subTest(slot_count=slot_count):
                case_name = str(slot_count)
                ceiling = baseline["cases"][case_name]["thresholds"][
                    "inlined_static_asset_bytes"
                ]
                baseline["cases"][case_name]["inlined_static_asset_bytes"] = ceiling + 1
                measured["cases"][case_name]["inlined_static_asset_bytes"] = ceiling + 1

                errors = compare_baselines(baseline, measured)

                self.assertTrue(
                    any(
                        f"case {case_name} inlined_static_asset_bytes exceeds hard ceiling"
                        in error
                        for error in errors
                    ),
                    errors,
                )

    def test_write_rejects_over_ceiling_static_assets_without_mutating_baseline(self) -> None:
        measured = build_baseline()
        case = measured["cases"]["60"]
        ceiling = 2_097_152
        case["thresholds"]["inlined_static_asset_bytes"] = ceiling
        case["inlined_static_asset_bytes"] = ceiling + 1

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline_path = root / "docs" / "performance-baseline-v1.json"
            baseline_path.parent.mkdir()
            original_bytes = b"existing reviewed baseline\n"
            baseline_path.write_bytes(original_bytes)

            with (
                patch.object(build_perf_baseline, "ROOT", root),
                patch.object(build_perf_baseline, "BASELINE_PATH", baseline_path),
                patch.object(build_perf_baseline, "build_baseline", return_value=measured),
                patch.object(sys, "argv", ["build_perf_baseline.py", "--write"]),
            ):
                result = build_perf_baseline.main()

            self.assertNotEqual(result, 0)
            self.assertEqual(baseline_path.read_bytes(), original_bytes)

    def test_baseline_comparison_keeps_query_invariants_exact(self) -> None:
        baseline = self._comparison_payload()
        measured = copy.deepcopy(baseline)
        measured["cases"]["60"]["query_count"] = 2  # type: ignore[index]

        errors = compare_baselines(baseline, measured)

        self.assertTrue(any("query_count changed" in error for error in errors), errors)

    def test_modeled_measurement_reports_inlined_static_assets_separately(self) -> None:
        from tests.perf_fixtures import measure_modeled_perf_case

        case = measure_modeled_perf_case(60)

        self.assertGreater(case["inlined_static_asset_bytes"], 0)
        self.assertEqual(
            case["export_html_bytes"],
            case["export_html_document_bytes"] + case["inlined_static_asset_bytes"],
        )

    def test_modeled_60_and_347_fixtures_are_deterministic_and_sanitized(self) -> None:
        for slot_count in MODELED_SLOT_COUNTS:
            with self.subTest(slot_count=slot_count):
                first_snapshot = build_modeled_inventory_snapshot(slot_count)
                second_snapshot = build_modeled_inventory_snapshot(slot_count)
                first_history = build_modeled_scope_history(slot_count)
                second_history = build_modeled_scope_history(slot_count)

                self.assertEqual(len(first_snapshot.slots), slot_count)
                self.assertEqual(first_snapshot.layout_slot_count, slot_count)
                self.assertEqual(first_snapshot.generated_at, FIXTURE_GENERATED_AT)
                self.assertEqual(first_snapshot.last_updated, FIXTURE_GENERATED_AT)
                self.assertEqual(
                    compact_json_bytes(first_snapshot.model_dump(mode="json")),
                    compact_json_bytes(second_snapshot.model_dump(mode="json")),
                )
                self.assertEqual(compact_json_bytes(first_history), compact_json_bytes(second_history))
                self.assertEqual(list(first_history), list(range(slot_count)))

                for slot, payload in first_history.items():
                    self.assertEqual(payload["slot"], slot)
                    self.assertEqual(len(payload["events"]), 1)
                    self.assertEqual(list(payload["metrics"]), list(HISTORY_METRIC_NAMES))
                    self.assertTrue(all(len(samples) == 2 for samples in payload["metrics"].values()))
                    self.assertEqual(sum(payload["sample_counts"].values()), 12)

                fixture_text = compact_json_bytes(
                    {
                        "snapshot": first_snapshot.model_dump(mode="json"),
                        "history": first_history,
                    }
                ).decode("utf-8")
                self.assertIsNone(PRIVATE_OR_SECRET_PATTERN.search(fixture_text))

    def test_inventory_snapshot_serialized_byte_budgets(self) -> None:
        byte_budgets = {
            slot_count: thresholds["inventory_response_bytes"]
            for slot_count, thresholds in MODELED_THRESHOLDS.items()
        }

        for slot_count, byte_budget in byte_budgets.items():
            with self.subTest(slot_count=slot_count):
                payload_bytes = compact_json_bytes(
                    build_modeled_inventory_snapshot(slot_count).model_dump(mode="json")
                )

                self.assertLessEqual(len(payload_bytes), byte_budget)

    def test_scope_history_query_budget_is_slot_count_independent(self) -> None:
        from tests.perf_fixtures import populate_modeled_history_store

        for slot_count in MODELED_SLOT_COUNTS:
            with self.subTest(slot_count=slot_count), tempfile.TemporaryDirectory() as temp_dir:
                store = HistoryStore(str(Path(temp_dir) / "history.db"))
                snapshot = build_modeled_inventory_snapshot(slot_count)
                populate_modeled_history_store(store, slot_count)
                connection_count = 0
                select_statements: list[str] = []
                original_connect = store._connect

                def traced_connect():
                    nonlocal connection_count
                    connection_count += 1
                    connection = original_connect()
                    connection.set_trace_callback(
                        lambda statement: select_statements.append(statement)
                        if statement.lstrip().upper().startswith("SELECT")
                        else None
                    )
                    return connection

                with patch.object(store, "_connect", side_effect=traced_connect):
                    histories = store.list_scope_history(
                        snapshot.selected_system_id or "",
                        snapshot.selected_enclosure_id,
                        slots=list(range(slot_count)),
                        event_limit=12,
                        metric_limits={metric_name: 2 for metric_name in HISTORY_METRIC_NAMES},
                    )

                self.assertEqual(list(histories), list(range(slot_count)))
                self.assertEqual(connection_count, 1)
                self.assertLessEqual(
                    len(select_statements),
                    MODELED_THRESHOLDS[slot_count]["scope_history_select_statements"],
                )
                for slot in (0, slot_count - 1):
                    self.assertEqual(len(histories[slot]["events"]), 1)
                    self.assertEqual(
                        histories[slot]["sample_counts"],
                        {metric: 2 for metric in HISTORY_METRIC_NAMES},
                    )

    def test_scope_history_compact_response_byte_budgets(self) -> None:
        from history_service import main as history_main
        from tests.perf_fixtures import populate_modeled_history_store

        byte_budgets = {
            slot_count: thresholds["scope_history_response_bytes"]
            for slot_count, thresholds in MODELED_THRESHOLDS.items()
        }
        for slot_count, byte_budget in byte_budgets.items():
            with self.subTest(slot_count=slot_count), tempfile.TemporaryDirectory() as temp_dir:
                store = HistoryStore(str(Path(temp_dir) / "history.db"))
                snapshot = build_modeled_inventory_snapshot(slot_count)
                populate_modeled_history_store(store, slot_count)
                with patch.object(history_main, "store", store):
                    payload = asyncio.run(
                        history_main.scope_slot_history(
                            system_id=snapshot.selected_system_id or "",
                            enclosure_id=snapshot.selected_enclosure_id,
                            slots=list(range(slot_count)),
                            metrics=list(HISTORY_METRIC_NAMES),
                            since=None,
                            event_limit=12,
                        )
                    )

                histories = payload["histories"]
                self.assertIsInstance(histories, dict)
                assert isinstance(histories, dict)
                self.assertEqual(list(histories), [str(slot) for slot in range(slot_count)])
                self.assertLessEqual(len(compact_json_bytes(payload)), byte_budget)

    def test_snapshot_export_html_cache_and_retained_byte_budgets(self) -> None:
        from tests.perf_fixtures import ModeledHistoryBackend, build_modeled_request

        html_byte_budgets = {
            slot_count: thresholds["export_html_bytes"]
            for slot_count, thresholds in MODELED_THRESHOLDS.items()
        }
        retained_byte_budgets = {
            slot_count: thresholds["logical_retained_bytes"]
            for slot_count, thresholds in MODELED_THRESHOLDS.items()
        }
        for slot_count in MODELED_SLOT_COUNTS:
            with self.subTest(slot_count=slot_count):
                EXPORT_HISTORY_CACHE.clear()
                EXPORT_RENDER_CACHE.clear()
                EXPORT_ZIP_CACHE.clear()
                snapshot = build_modeled_inventory_snapshot(slot_count)
                history_backend = ModeledHistoryBackend(slot_count)
                exporter = SnapshotExportService(Settings(), history_backend, templates)
                render_calls = 0
                zip_build_calls = 0
                original_render = exporter._render_template_with_assets
                original_zip_builder = exporter._build_zip_archive

                def counting_render(*args, **kwargs):
                    nonlocal render_calls
                    render_calls += 1
                    return original_render(*args, **kwargs)

                def counting_zip_builder(*args, **kwargs):
                    nonlocal zip_build_calls
                    zip_build_calls += 1
                    return original_zip_builder(*args, **kwargs)

                exporter._render_template_with_assets = counting_render  # type: ignore[method-assign]
                exporter._build_zip_archive = counting_zip_builder  # type: ignore[method-assign]
                arguments = {
                    "request": build_modeled_request(),
                    "snapshot": snapshot,
                    "smart_summary_cache": {},
                    "selected_slot": 0,
                    "history_window_hours": 24,
                    "history_panel_open": False,
                    "io_chart_mode": "total",
                    "generated_at": FIXTURE_GENERATED_AT,
                }

                first = asyncio.run(exporter.build_enclosure_snapshot_html(**arguments))
                second = asyncio.run(exporter.build_enclosure_snapshot_html(**arguments))
                html_bytes = first.html.encode("utf-8")
                history_cache_bytes = sum(entry.size_bytes for entry in EXPORT_HISTORY_CACHE.values())
                render_cache_bytes = sum(entry.size_bytes for entry in EXPORT_RENDER_CACHE.values())
                zip_cache_bytes = sum(entry.size_bytes for entry in EXPORT_ZIP_CACHE.values())
                retained_bytes = history_cache_bytes + render_cache_bytes + zip_cache_bytes

                self.assertIs(first, second)
                self.assertLessEqual(len(html_bytes), html_byte_budgets[slot_count])
                self.assertLessEqual(retained_bytes, retained_byte_budgets[slot_count])
                self.assertLessEqual(retained_bytes, exporter.settings.app.export_cache_max_bytes)
                self.assertEqual(history_backend.status_calls, 1)
                self.assertEqual(history_backend.scope_history_calls, 1)
                self.assertEqual(history_backend.slot_history_calls, 0)
                self.assertEqual(render_calls, 1)
                self.assertEqual(zip_build_calls, 0)
                self.assertEqual(len(EXPORT_HISTORY_CACHE), 1)
                self.assertEqual(len(EXPORT_RENDER_CACHE), 1)
                self.assertEqual(len(EXPORT_ZIP_CACHE), 0)

    def test_checked_in_perf_baseline_matches_generated_deterministic_metrics(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/build_perf_baseline.py", "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        baseline = json.loads((ROOT / "docs" / "performance-baseline-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["schema_version"], 3)
        self.assertEqual(baseline["fixture_version"], FIXTURE_VERSION)
        self.assertTrue(baseline["modeled"])
        self.assertEqual(baseline["wall_clock_policy"], "report-only")
        self.assertEqual(list(baseline["cases"]), [str(slot_count) for slot_count in MODELED_SLOT_COUNTS])

        required_metrics = {
            "slot_count",
            "inventory_response_bytes",
            "scope_history_response_bytes",
            "scope_history_connections",
            "scope_history_select_statements",
            "history_status_calls",
            "history_scope_calls",
            "history_slot_calls",
            "render_calls",
            "zip_build_calls",
            "history_cache_entries",
            "history_cache_bytes",
            "render_cache_entries",
            "render_cache_bytes",
            "zip_cache_entries",
            "zip_cache_bytes",
            "export_cache_total_bytes",
            "export_cache_max_bytes",
            "export_html_bytes",
            "export_html_document_bytes",
            "inlined_static_asset_bytes",
            "logical_retained_bytes",
            "thresholds",
        }
        for slot_count in MODELED_SLOT_COUNTS:
            case = baseline["cases"][str(slot_count)]
            self.assertEqual(set(case), required_metrics)
            self.assertEqual(case["slot_count"], slot_count)
            self.assertEqual(case["thresholds"], MODELED_THRESHOLDS[slot_count])
            self.assertLessEqual(
                case["scope_history_select_statements"],
                MODELED_THRESHOLDS[slot_count]["scope_history_select_statements"],
            )
            self.assertEqual(case["history_scope_calls"], 1)
            self.assertEqual(case["history_slot_calls"], 0)
            self.assertEqual(case["zip_build_calls"], 0)
            self.assertEqual(
                case["export_cache_total_bytes"],
                case["history_cache_bytes"] + case["render_cache_bytes"] + case["zip_cache_bytes"],
            )
            self.assertLessEqual(case["export_cache_total_bytes"], case["export_cache_max_bytes"])
            self.assertEqual(
                case["export_html_bytes"],
                case["export_html_document_bytes"] + case["inlined_static_asset_bytes"],
            )
            self.assertNotIn("duration_budget_ms", case)

    def test_performance_budget_docs_define_modeled_scope_and_refresh_policy(self) -> None:
        documentation = (ROOT / "docs" / "PERFORMANCE_BUDGETS.md").read_text(encoding="utf-8").lower()

        for marker in (
            "modeled fixtures",
            "60",
            "347",
            "wall-clock durations are report-only",
            "python scripts/build_perf_baseline.py --check",
            "python scripts/build_perf_baseline.py --write",
            "10%",
            "4,096 bytes",
            "inlined static asset bytes",
            "hard ceilings always apply",
            "shared 32 mib",
            "oversized entries are returned but not cached",
            "snapshot_export_cache_bytes",
            "python scripts/benchmark_snapshot_export_cache.py",
            "selection and focus updates must not rebuild",
            "parser and sas diagnostic payloads remain bounded",
            "browser history caches remain bounded",
            "refresh paths preserve cached state",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, documentation)

        self.assertIsNone(re.search(r"#(?:36|48|55|56)\b", documentation))

    def test_report_only_snapshot_export_cache_benchmark_uses_modeled_fixture(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/benchmark_snapshot_export_cache.py",
                "--slots",
                "60",
                "--iterations",
                "1",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["policy"], "report-only")
        self.assertEqual(payload["iterations"], 1)
        self.assertEqual(list(payload["cases"]), ["60"])
        case = payload["cases"]["60"]
        self.assertEqual(case["slot_count"], 60)
        self.assertGreaterEqual(case["elapsed_ms"], 0)
        self.assertLessEqual(case["export_cache_total_bytes"], case["export_cache_max_bytes"])

    def test_python_ci_does_not_repeat_baseline_check_outside_unittest(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        python_steps = workflow["jobs"]["python-source"]["steps"]
        run_commands = "\n".join(str(step.get("run") or "") for step in python_steps)

        self.assertNotIn("python scripts/build_perf_baseline.py --check", run_commands)


if __name__ == "__main__":
    unittest.main()
