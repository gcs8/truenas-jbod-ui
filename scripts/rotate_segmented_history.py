from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLATFORM_HINT = (
    "This tool needs Linux (POSIX file locking). Run it inside the container: "
    "docker compose run --rm enclosure-history python "
    "/app/scripts/rotate_segmented_history.py --source /history/history.db "
    "--segments-dir /history/segments --recover --apply"
)


def _load_rotation_api():
    """Import the rotation API after argparse, so --help works off Linux."""
    try:
        from history_service.segment_rotation import (
            recover_pending_rotation,
            rotate_segmented_history,
        )
    except (ImportError, RuntimeError) as exc:
        raise SystemExit(PLATFORM_HINT + "\n(" + str(exc) + ")") from exc
    return recover_pending_rotation, rotate_segmented_history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append or recover one later segmented-history generation.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to the live SQLite history database, e.g. /history/history.db.",
    )
    parser.add_argument(
        "--segments-dir",
        type=Path,
        required=True,
        help="Directory holding the sealed segments and their catalog, e.g. /history/segments.",
    )
    parser.add_argument(
        "--cutoff",
        help=(
            "ISO-8601 timestamp with a timezone offset, e.g. 2026-01-01T00:00:00+00:00; "
            "rows strictly before it move into the new segment. Required unless --recover."
        ),
    )
    parser.add_argument(
        "--key-id",
        help=(
            "Key identifier recorded in the receipt, e.g. archive-key-1. Metadata only: it does "
            "not encrypt or sign the segment. Required unless --recover."
        ),
    )
    parser.add_argument(
        "--scheduled-backup-dir",
        type=Path,
        help="Directory the scheduled backup writes into, e.g. /history/backup. Required unless --recover.",
    )
    parser.add_argument(
        "--scheduled-backup-status",
        type=Path,
        help="Scheduled backup status file, e.g. /backup-status/status.json. Required unless --recover.",
    )
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
    recover_pending_rotation, rotate_segmented_history = _load_rotation_api()
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
