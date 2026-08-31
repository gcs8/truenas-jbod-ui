from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history_service.segment_sealer import seal_history_segment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seal a quiesced history database into one immutable SQLite segment.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--sequence", type=int, default=1)
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
