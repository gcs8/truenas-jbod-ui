"""Backup archive health for the main UI ``/healthz`` (#578).

The backup scheduler writes a secret-free status file
(``BACKUP_ARCHIVE_STATUS_FILE``, default ``/app/backup-status/backup-archive.json``).
A failed last run for a class, or a failed last copy to a remote target, is a
*degraded* reason: the UI keeps serving and ``/healthz`` still answers 200.
A missing status file means backups are not in use and is not a problem.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STATUS_FILE = "/app/backup-status/backup-archive.json"
_MAX_DETAIL = 160


def _status_path() -> Path:
    return Path((os.getenv("BACKUP_ARCHIVE_STATUS_FILE") or "").strip() or DEFAULT_STATUS_FILE)


def _short(text: object) -> str:
    value = " ".join(str(text or "no details recorded").split())
    return value if len(value) <= _MAX_DETAIL else value[: _MAX_DETAIL - 1] + "…"


def backup_archive_problems(path: str | Path | None = None) -> list[str]:
    from history_service.backup_scheduler.service import read_archive_status

    try:
        payload = read_archive_status(path or _status_path())
    except Exception:  # noqa: BLE001 - health must never fail on an advisory file
        return []
    if not payload:
        return []
    problems: list[str] = []
    classes = payload.get("classes") or {}
    classes = classes if isinstance(classes, dict) else {}
    for backup_class in ("config", "full"):
        run = classes.get(backup_class)
        if isinstance(run, dict) and run.get("ok") is False:
            problems.append(f"{backup_class.capitalize()} backup failed: {_short(run.get('detail'))}")
    targets = payload.get("targets") or {}
    targets = targets if isinstance(targets, dict) else {}
    for target_id in sorted(targets):
        run = targets[target_id]
        if isinstance(run, dict) and run.get("ok") is False:
            label = _short(run.get("label") or target_id)
            problems.append(f"Backup target {label} degraded: {_short(run.get('detail'))}")
    return problems
