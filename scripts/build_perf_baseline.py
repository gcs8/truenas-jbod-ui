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


def build_baseline() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "fixture_version": FIXTURE_VERSION,
        "modeled": True,
        "wall_clock_policy": "report-only",
        "cases": {
            str(slot_count): measure_modeled_perf_case(slot_count)
            for slot_count in MODELED_SLOT_COUNTS
        },
    }


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

    expected = render_baseline(build_baseline())
    if args.write:
        write_baseline(expected)
        print(f"Wrote {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    if not BASELINE_PATH.exists():
        print(f"Missing {BASELINE_PATH.relative_to(ROOT)}; run with --write.", file=sys.stderr)
        return 1
    actual = BASELINE_PATH.read_bytes()
    if actual != expected:
        print(f"Stale {BASELINE_PATH.relative_to(ROOT)}; review changes and run with --write.", file=sys.stderr)
        return 1
    print(f"Performance baseline matches: {BASELINE_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
