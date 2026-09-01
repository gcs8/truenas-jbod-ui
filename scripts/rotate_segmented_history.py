from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history_service.segment_rotation import recover_pending_rotation, rotate_segmented_history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append or recover one later segmented-history generation.",
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--cutoff")
    parser.add_argument("--key-id")
    parser.add_argument("--scheduled-backup-dir", type=Path)
    parser.add_argument("--scheduled-backup-status", type=Path)
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Recover the exact journal-authenticated pending rotation.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Publish or recover the journal-authenticated generation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.recover:
        receipt = recover_pending_rotation(
            source=args.source,
            segments_directory=args.segments_dir,
            apply=args.apply,
        )
    else:
        if any(
            value is None
            for value in (
                args.cutoff,
                args.key_id,
                args.scheduled_backup_dir,
                args.scheduled_backup_status,
            )
        ):
            raise SystemExit(
                "--cutoff, --key-id, --scheduled-backup-dir, and "
                "--scheduled-backup-status are required unless --recover is used."
            )
        receipt = rotate_segmented_history(
            source=args.source,
            segments_directory=args.segments_dir,
            cutoff=args.cutoff,
            key_id=args.key_id,
            scheduled_backup_directory=args.scheduled_backup_dir,
            scheduled_backup_status_path=args.scheduled_backup_status,
            apply=args.apply,
        )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
