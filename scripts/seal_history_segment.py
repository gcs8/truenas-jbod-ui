from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history_service.segment_sealer import seal_history_segment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seal a quiesced history database into one immutable SQLite segment.")
    parser.add_argument("--source", type=Path, required=True, help="Path to a quiesced SQLite history database, e.g. /history/history.db; stop writers first.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Segment output directory, e.g. /history/segments; creates <segment-id>.sqlite3 without overwriting an existing segment.")
    parser.add_argument("--segment-id", required=True, help="1-128 characters: start with an ASCII letter or digit, then letters, digits, dots, underscores or hyphens, e.g. segment-0001.")
    parser.add_argument("--cutoff", required=True, help="ISO-8601 timestamp with timezone offset, e.g. 2026-01-01T00:00:00+00:00; normalized to UTC midnight, keeping rows strictly before that boundary.")
    parser.add_argument("--key-id", required=True, help="Nonempty key identifier for receipt metadata, at most 128 characters, e.g. archive-key-1; not secret key material and does not encrypt or sign the segment.")
    parser.add_argument("--sequence", type=int, default=1, help="Receipt sequence as a positive integer, e.g. 2 (default: 1).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    receipt = seal_history_segment(
        source=args.source,
        output_directory=args.output_dir,
        segment_id=args.segment_id,
        cutoff=args.cutoff,
        key_id=args.key_id,
        sequence=args.sequence,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
