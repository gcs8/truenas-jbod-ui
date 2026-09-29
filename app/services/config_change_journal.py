"""Append config-change journal entries after a config write (#573).

Every config mutation point (mappings, SAS aliases, systems, storage views,
profiles, runtime overrides, restore) calls :func:`record_config_change` *after*
its write is durable. The backup scheduler sidecar reads the journal and turns a
burst of changes into one config-only backup.

The hook must never fail or noticeably slow the save that called it:

* it does nothing unless config backups are enabled
  (``BACKUP_CONFIG_ENABLED`` or ``backups.config.enabled`` in config.yaml);
* it does nothing if the journal directory has not been provisioned;
* any journal error is logged (type name only) and swallowed.

Journal location and permissions: ``BACKUP_JOURNAL_PATH`` (default
``/app/backup-journal/config-changes.jsonl``). The directory is shared by the
UI, the admin sidecar and the backup scheduler, which run as different users
that share ``APP_GID``; provision it setgid ``2770`` owned by ``APP_GID``.
Journal files are created ``0660`` so every member of that group can append.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_JOURNAL_PATH = "/app/backup-journal/config-changes.jsonl"
JOURNAL_FILE_MODE = 0o660
MAX_SUBJECT_CHARS = 200

_journals: dict[str, Any] = {}
_journals_lock = threading.Lock()
_missing_dir_logged: set[str] = set()


def journal_path() -> Path:
    return Path(os.getenv("BACKUP_JOURNAL_PATH", "").strip() or DEFAULT_JOURNAL_PATH)


_yaml_cache: dict[str, tuple[tuple[int, int], dict | None]] = {}


def _yaml_backups_section() -> dict | None:
    """``backups`` from config.yaml, re-read only when the file changes."""

    import yaml

    path = Path(os.getenv("APP_CONFIG_PATH") or "/app/config/config.yaml")
    try:
        metadata = path.stat()
    except OSError:
        return None
    key = (metadata.st_mtime_ns, metadata.st_size)
    cached = _yaml_cache.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    section = loaded.get("backups") if isinstance(loaded, dict) else None
    section = section if isinstance(section, dict) else None
    _yaml_cache[str(path)] = (key, section)
    return section


def config_backups_enabled() -> bool:
    """``BACKUP_CONFIG_ENABLED`` wins; otherwise ``backups.config.enabled`` in config.yaml."""

    from history_service.backup_archive.policy import config_backups_enabled_from_environment

    raw = os.getenv("BACKUP_CONFIG_ENABLED")
    if raw is not None and raw.strip():
        return config_backups_enabled_from_environment(None)
    try:
        section = _yaml_backups_section()
    except Exception:  # noqa: BLE001 - never let the hook break a save
        return False
    return config_backups_enabled_from_environment(section)


def _journal_for(path: Path) -> Any:
    from history_service.backup_archive.journal import ChangeJournal

    key = str(path)
    with _journals_lock:
        journal = _journals.get(key)
        if journal is None:
            journal = ChangeJournal(path, file_mode=JOURNAL_FILE_MODE)
            _journals[key] = journal
        return journal


def record_config_change(action: str, subject: str = "") -> bool:
    """Journal one config mutation. Returns True when an entry was written."""

    try:
        if not config_backups_enabled():
            return False
        path = journal_path()
        if not path.parent.is_dir():
            if str(path.parent) not in _missing_dir_logged:
                _missing_dir_logged.add(str(path.parent))
                logger.warning(
                    "Config backups are enabled but the change journal directory is missing; "
                    "changes are not journalled until it is provisioned."
                )
            return False
        clean_subject = " ".join(str(subject or "").split())[:MAX_SUBJECT_CHARS]
        _journal_for(path).append(action, clean_subject)
        return True
    except Exception as exc:  # noqa: BLE001 - the save already succeeded; never fail it
        logger.warning("Config change journal append failed (%s); the save is unaffected.", type(exc).__name__)
        return False


def reset_for_tests() -> None:
    with _journals_lock:
        _journals.clear()
    _missing_dir_logged.clear()
    _yaml_cache.clear()
