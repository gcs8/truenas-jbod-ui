from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history_service.segment_reader import SegmentedHistoryReader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read slot events from a hot history database and immutable segments.")
    parser.add_argument("--hot", type=Path, required=True)
    segment_source = parser.add_mutually_exclusive_group()
    segment_source.add_argument("--segment", type=Path, action="append", default=[])
    segment_source.add_argument("--catalog", type=Path)
    parser.add_argument("--kind", choices=("slot-events", "raw-metric-samples", "metric-samples"), default="slot-events")
    parser.add_argument("--system-id", required=True)
    parser.add_argument("--enclosure-id")
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--metric-name")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--since")
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Read the hot database beside a running history service with shared SQLite locking. "
            "By default the hot database is opened immutable, which never creates -wal/-shm "
            "sidecars on a quiesced database, and one that already has sidecars is refused."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reader = (
        SegmentedHistoryReader.from_catalog(
            hot_path=args.hot,
            catalog_path=args.catalog,
            quiesced_hot=not args.live,
        )
        if args.catalog is not None
        else SegmentedHistoryReader(
            hot_path=args.hot,
            segment_paths=args.segment,
            quiesced_hot=not args.live,
        )
    )
    payload = (
        reader.list_raw_metric_samples(
            args.system_id,
            args.enclosure_id,
            args.slot,
            metric_name=args.metric_name,
            limit=args.limit,
            since=args.since,
        )
        if args.kind == "raw-metric-samples"
        else reader.list_metric_samples(
            args.system_id,
            args.enclosure_id,
            args.slot,
            metric_name=args.metric_name,
            limit=args.limit,
            since=args.since,
        )
        if args.kind == "metric-samples"
        else reader.list_slot_events(
            args.system_id,
            args.enclosure_id,
            args.slot,
            limit=args.limit,
            since=args.since,
        )
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
