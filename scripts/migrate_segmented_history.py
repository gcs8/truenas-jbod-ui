from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history_service.segment_migration import (
    migrate_segmented_history,
    recover_pending_migration,
    rollback_segmented_history,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate or roll back one quiesced segmented history database.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--cutoff")
    parser.add_argument("--key-id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rollback", action="store_true", help="Restore the retained v1 database and remove the cataloged generation.")
    mode.add_argument(
        "--recover-rollback",
        action="store_true",
        help="Restore v1 after an interrupted pre-catalog migration and report unreferenced segments.",
    )
    parser.add_argument("--apply", action="store_true", help="Publish the segment, hot replacement, and catalog.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.rollback:
        receipt = rollback_segmented_history(
            source=args.source,
            segments_directory=args.segments_dir,
            apply=args.apply,
        )
    elif args.recover_rollback:
        receipt = recover_pending_migration(
            source=args.source,
            segments_directory=args.segments_dir,
            apply=args.apply,
        )
    else:
        if args.cutoff is None or args.key_id is None:
            raise SystemExit("--cutoff and --key-id are required unless a rollback mode is used.")
        receipt = migrate_segmented_history(
            source=args.source,
            segments_directory=args.segments_dir,
            cutoff=args.cutoff,
            key_id=args.key_id,
            apply=args.apply,
        )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
