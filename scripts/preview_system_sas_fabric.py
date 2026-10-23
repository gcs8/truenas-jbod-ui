#!/usr/bin/env python3
"""Read-only local preview of the inactive system SAS graph draft.

Input is a JSON array of existing SasFabricSnapshot.model_dump(mode="json")
objects. This command reads only stdin or the explicitly named input file and
writes deterministic JSON to stdout. No config, credentials or network access.
Budgets are required preview choices, not application resource-policy defaults.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.system_sas_fabric import compose_system_sas_fabrics  # noqa: E402


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="JSON input path; omit or use - for stdin")
    parser.add_argument("--max-input-bytes", type=int, required=True, help="explicit local preview input budget")
    parser.add_argument("--max-depth", type=int, required=True, help="explicit local preview JSON depth budget")
    args = parser.parse_args(argv)
    try:
        if args.max_input_bytes <= 0 or args.max_depth <= 0:
            raise ValueError("preview budgets must be positive integers")
        if args.input in (None, "-"):
            data = sys.stdin.buffer.read(args.max_input_bytes + 1)
        else:
            with Path(args.input).open("rb") as handle:
                data = handle.read(args.max_input_bytes + 1)
        if len(data) > args.max_input_bytes:
            raise ValueError("preview input byte budget exceeded")
        payloads = json.loads(data, object_pairs_hook=_unique_object)
        result = compose_system_sas_fabrics(payloads, max_input_bytes=args.max_input_bytes, max_depth=args.max_depth)
        output = json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (OSError, ValueError, RecursionError, OverflowError) as exc:
        # Do not echo input fields or filesystem exceptions containing a path.
        print("system SAS draft preview refused input: " + type(exc).__name__, file=sys.stderr)
        return 2
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
