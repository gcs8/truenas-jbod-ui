from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLATFORM_HINT = (
    "This tool needs Linux (POSIX file locking). Run it inside the container: "
    "docker compose run --rm enclosure-history python "
    "/app/scripts/migrate_segmented_history.py --source /history/history.db "
    "--segments-dir /history/segments --recover-rollback --apply"
)


def _load_migration_api():
    """Import the migration API after argparse, so --help works off Linux."""
    try:
        from history_service.segment_migration import (
            migrate_segmented_history,
            recover_pending_migration,
            rollback_segmented_history,
        )
    except (ImportError, RuntimeError) as exc:
        raise SystemExit(PLATFORM_HINT + "\n(" + str(exc) + ")") from exc
    return migrate_segmented_history, recover_pending_migration, rollback_segmented_history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate or roll back one quiesced segmented history database.")
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to the quiesced SQLite history database, e.g. /history/history.db; stop writers first.",
    )
    parser.add_argument(
        "--segments-dir",
        type=Path,
        required=True,
        help="Directory to write the sealed segments and their catalog into, e.g. /history/segments.",
    )
    parser.add_argument(
        "--cutoff",
        help=(
            "ISO-8601 timestamp with a timezone offset, e.g. 2026-01-01T00:00:00+00:00; "
            "rows strictly before it move into the segment. Required unless a rollback mode is used."
        ),
    )
    parser.add_argument(
        "--key-id",
        help=(
            "Key identifier recorded in the receipt, e.g. archive-key-1. Metadata only: it does "
            "not encrypt or sign the segment. Required unless a rollback mode is used."
        ),
    )
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
    (
        migrate_segmented_history,
        recover_pending_migration,
        rollback_segmented_history,
    ) = _load_migration_api()
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
