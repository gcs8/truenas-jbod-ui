from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.perf_fixtures import (  # noqa: E402
    FIXTURE_VERSION,
    MODELED_SLOT_COUNTS,
    measure_modeled_perf_case,
)


BASELINE_PATH = ROOT / "docs" / "performance-baseline-v1.json"
SCHEMA_VERSION = 3
BYTE_DRIFT_PERCENT = 10
MINIMUM_BYTE_DRIFT = 4096
EXACT_METRICS = (
    "slot_count",
    "scope_history_connections",
    "scope_history_select_statements",
    "history_status_calls",
    "history_scope_calls",
    "history_slot_calls",
    "render_calls",
    "zip_build_calls",
    "history_cache_entries",
    "render_cache_entries",
    "zip_cache_entries",
    "export_cache_max_bytes",
)
BYTE_METRICS = (
    "inventory_response_bytes",
    "scope_history_response_bytes",
    "history_cache_bytes",
    "render_cache_bytes",
    "zip_cache_bytes",
    "export_cache_total_bytes",
    "export_html_bytes",
    "export_html_document_bytes",
    "inlined_static_asset_bytes",
    "logical_retained_bytes",
)


def comparison_policy() -> dict[str, Any]:
    return {
        "exact_metrics": list(EXACT_METRICS),
        "byte_metrics": list(BYTE_METRICS),
        "byte_drift_percent": BYTE_DRIFT_PERCENT,
        "minimum_byte_drift": MINIMUM_BYTE_DRIFT,
        "byte_drift_direction": "symmetric",
    }


def build_baseline() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "modeled": True,
        "wall_clock_policy": "report-only",
        "comparison_policy": comparison_policy(),
        "cases": {
            str(slot_count): measure_modeled_perf_case(slot_count)
            for slot_count in MODELED_SLOT_COUNTS
        },
    }


def _is_measurement(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def compare_baselines(baseline: dict[str, Any], measured: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    metadata_keys = (
        "schema_version",
        "fixture_version",
        "modeled",
        "wall_clock_policy",
        "comparison_policy",
    )
    for key in metadata_keys:
        if baseline.get(key) != measured.get(key):
            errors.append(
                f"{key} changed: baseline={baseline.get(key)!r}, measured={measured.get(key)!r}"
            )

    policy = measured.get("comparison_policy")
    if not isinstance(policy, dict):
        errors.append("comparison_policy must be an object")
        return errors
    exact_metrics = policy.get("exact_metrics")
    byte_metrics = policy.get("byte_metrics")
    drift_percent = policy.get("byte_drift_percent")
    minimum_drift = policy.get("minimum_byte_drift")
    if not isinstance(exact_metrics, list) or not all(
        isinstance(metric, str) for metric in exact_metrics
    ):
        errors.append("comparison_policy.exact_metrics must be a list of metric names")
        return errors
    if not isinstance(byte_metrics, list) or not all(
        isinstance(metric, str) for metric in byte_metrics
    ):
        errors.append("comparison_policy.byte_metrics must be a list of metric names")
        return errors
    if not isinstance(drift_percent, int) or isinstance(drift_percent, bool) or drift_percent < 0:
        errors.append("comparison_policy.byte_drift_percent must be a non-negative integer")
        return errors
    if not isinstance(minimum_drift, int) or isinstance(minimum_drift, bool) or minimum_drift < 0:
        errors.append("comparison_policy.minimum_byte_drift must be a non-negative integer")
        return errors
    if policy.get("byte_drift_direction") != "symmetric":
        errors.append("comparison_policy.byte_drift_direction must be 'symmetric'")
        return errors

    baseline_cases = baseline.get("cases")
    measured_cases = measured.get("cases")
    if not isinstance(baseline_cases, dict) or not isinstance(measured_cases, dict):
        errors.append("cases must be an object")
        return errors
    if set(baseline_cases) != set(measured_cases):
        errors.append(
            "modeled cases changed: "
            f"baseline={sorted(baseline_cases)}, measured={sorted(measured_cases)}"
        )

    classified_metrics = set(exact_metrics) | set(byte_metrics)
    for case_name in sorted(set(baseline_cases) & set(measured_cases)):
        baseline_case = baseline_cases[case_name]
        measured_case = measured_cases[case_name]
        if not isinstance(baseline_case, dict) or not isinstance(measured_case, dict):
            errors.append(f"case {case_name} must be an object")
            continue
        if set(baseline_case) != set(measured_case):
            errors.append(
                f"case {case_name} metrics changed: "
                f"baseline={sorted(baseline_case)}, measured={sorted(measured_case)}"
            )
        measured_metric_names = set(measured_case) - {"thresholds"}
        unclassified_metrics = measured_metric_names - classified_metrics
        missing_metrics = classified_metrics - measured_metric_names
        if unclassified_metrics:
            errors.append(
                f"case {case_name} has unclassified metrics: {sorted(unclassified_metrics)}"
            )
        if missing_metrics:
            errors.append(f"case {case_name} is missing metrics: {sorted(missing_metrics)}")

        baseline_thresholds = baseline_case.get("thresholds")
        measured_thresholds = measured_case.get("thresholds")
        if baseline_thresholds != measured_thresholds:
            errors.append(
                f"case {case_name} thresholds changed: "
                f"baseline={baseline_thresholds!r}, measured={measured_thresholds!r}"
            )

        for metric in exact_metrics:
            if metric not in baseline_case or metric not in measured_case:
                continue
            if baseline_case[metric] != measured_case[metric]:
                errors.append(
                    f"case {case_name} {metric} changed: "
                    f"baseline={baseline_case[metric]!r}, measured={measured_case[metric]!r}"
                )

        for metric in byte_metrics:
            if metric not in baseline_case or metric not in measured_case:
                continue
            baseline_value = baseline_case[metric]
            measured_value = measured_case[metric]
            if not _is_measurement(baseline_value) or not _is_measurement(measured_value):
                errors.append(f"case {case_name} {metric} must be an integer byte measurement")
                continue
            allowed_drift = max(
                minimum_drift,
                (abs(baseline_value) * drift_percent + 99) // 100,
            )
            if abs(measured_value - baseline_value) > allowed_drift:
                errors.append(
                    f"case {case_name} {metric} drifted beyond ±{allowed_drift} bytes: "
                    f"baseline={baseline_value}, measured={measured_value}"
                )

        if not isinstance(measured_thresholds, dict):
            errors.append(f"case {case_name} thresholds must be an object")
            continue
        for metric, ceiling in measured_thresholds.items():
            measured_value = measured_case.get(metric)
            if not _is_measurement(ceiling) or not _is_measurement(measured_value):
                errors.append(f"case {case_name} hard ceiling for {metric} must use integers")
                continue
            if measured_value > ceiling:
                errors.append(
                    f"case {case_name} {metric} exceeds hard ceiling: "
                    f"measured={measured_value}, ceiling={ceiling}"
                )

    return errors


def render_baseline(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode("utf-8")


def write_baseline(content: bytes) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{BASELINE_PATH.name}.",
        suffix=".tmp",
        dir=BASELINE_PATH.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, BASELINE_PATH)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check or refresh deterministic modeled performance budgets."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="atomically refresh docs/performance-baseline-v1.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the artifact instead of writing it; this is the default",
    )
    args = parser.parse_args()
    if args.write and args.check:
        parser.error("choose either --check or --write")

    measured = build_baseline()
    if args.write:
        write_baseline(render_baseline(measured))
        print(f"Wrote {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    if not BASELINE_PATH.exists():
        print(f"Missing {BASELINE_PATH.relative_to(ROOT)}; run with --write.", file=sys.stderr)
        return 1
    actual_bytes = BASELINE_PATH.read_bytes()
    try:
        baseline = json.loads(actual_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"Invalid {BASELINE_PATH.relative_to(ROOT)}: {exc}", file=sys.stderr)
        return 1
    if actual_bytes != render_baseline(baseline):
        print(
            f"Non-canonical {BASELINE_PATH.relative_to(ROOT)}; review changes and run with --write.",
            file=sys.stderr,
        )
        return 1
    errors = compare_baselines(baseline, measured)
    if errors:
        print(
            f"Stale {BASELINE_PATH.relative_to(ROOT)}; review changes and run with --write.",
            file=sys.stderr,
        )
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Performance baseline is within policy: {BASELINE_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
