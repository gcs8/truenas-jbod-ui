"""Operator recovery for the history database (#417).

Run inside the history container:

    docker compose exec enclosure-history python -m history_service.recovery status
    docker compose exec enclosure-history python -m history_service.recovery acknowledge

``status`` only reads. ``acknowledge`` first runs ``PRAGMA quick_check`` on the
live database, read-only, and refuses while it fails. It then clears the
collection pause marker and acknowledges a pending quarantine. It never
touches quarantined ``*.broken-*`` files and never deletes or rewrites history
rows. This adds no network endpoint.

Recover first: restore a full backup from the admin Backups page, or replace
the database with a known-good copy, then acknowledge.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from history_service.config import get_history_settings
from history_service.store import HistoryStore

EXIT_OK = 0
EXIT_REFUSED = 2


def _open_store() -> HistoryStore:
    # initialize=False: never run schema setup or quarantine logic from here.
    settings = get_history_settings()
    return HistoryStore(
        settings.sqlite_path,
        segment_catalog_path=settings.segment_catalog_path,
        shared_dir_mode=settings.shared_dir_mode,
        shared_file_mode=settings.shared_file_mode,
        initialize=False,
    )


def _status(store: HistoryStore) -> dict[str, object]:
    paused, paused_at = store.read_collection_pause()
    check = store.quick_check()
    try:
        recovery = store.quarantine_recovery_status()
    except Exception:  # noqa: BLE001 - a damaged database still gets a status line.
        recovery = {"history_recovery_required": True, "history_quarantined_at": None}
    return {
        "database_check": check,
        "collection_paused": paused,
        "collection_paused_at": paused_at.isoformat() if paused_at else None,
        "recovery_required": bool(recovery.get("history_recovery_required")),
        "quarantined_at": recovery.get("history_quarantined_at"),
    }


def main(argv: Sequence[str] | None = None, *, store: HistoryStore | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m history_service.recovery",
        description=(
            "Show or acknowledge history database recovery. Acknowledge only after "
            "restoring a good database; it refuses while the integrity check fails."
        ),
    )
    parser.add_argument("action", choices=("status", "acknowledge"))
    args = parser.parse_args(argv)
    store = store or _open_store()
    before = _status(store)
    if args.action == "status":
        print(json.dumps(before, indent=2, sort_keys=True))
        return EXIT_OK

    if before["database_check"] != "ok":
        print(
            "Refusing to acknowledge: the history database fails its integrity check "
            f"({before['database_check']}). Restore a full backup first, then run this again.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    cleared_pause = store.clear_collection_pause()
    acknowledged_quarantine = (
        store.acknowledge_quarantine_recovery() if before["recovery_required"] else False
    )
    after = _status(store)
    print(
        json.dumps(
            {
                **after,
                "cleared_collection_pause": cleared_pause,
                "acknowledged_quarantine": acknowledged_quarantine,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
