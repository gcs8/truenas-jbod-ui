from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.perf_fixtures import MODELED_SLOT_COUNTS, measure_modeled_perf_case  # noqa: E402


CACHE_FIELDS = (
    "slot_count",
    "history_cache_entries",
    "history_cache_bytes",
    "render_cache_entries",
    "render_cache_bytes",
    "zip_cache_entries",
    "zip_cache_bytes",
    "export_cache_total_bytes",
    "export_cache_max_bytes",
)


def benchmark_case(slot_count: int, iterations: int) -> dict[str, Any]:
    elapsed_samples: list[float] = []
    measured: dict[str, Any] | None = None
    for _iteration in range(iterations):
        started = time.perf_counter()
        current = measure_modeled_perf_case(slot_count)
        elapsed_samples.append((time.perf_counter() - started) * 1000)
        if measured is None:
            measured = current
            continue
        for field in CACHE_FIELDS:
            if current[field] != measured[field]:
                raise RuntimeError(f"Modeled benchmark field changed across iterations: {field}")
    assert measured is not None
    result = {field: measured[field] for field in CACHE_FIELDS}
    result["elapsed_ms"] = round(statistics.median(elapsed_samples), 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report modeled snapshot-export cache bytes and wall-clock latency.",
    )
    parser.add_argument(
        "--slots",
        nargs="+",
        type=int,
        choices=MODELED_SLOT_COUNTS,
        default=list(MODELED_SLOT_COUNTS),
        help="Modeled slot counts to benchmark.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Iterations per slot count; elapsed_ms is the median.",
    )
    args = parser.parse_args()
    if args.iterations <= 0:
        parser.error("--iterations must be greater than zero")

    payload = {
        "policy": "report-only",
        "iterations": args.iterations,
        "cases": {
            str(slot_count): benchmark_case(slot_count, args.iterations)
            for slot_count in args.slots
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
