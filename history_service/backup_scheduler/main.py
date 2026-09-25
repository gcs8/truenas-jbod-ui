"""Entrypoint for the ``enclosure-backup-scheduler`` sidecar.

Environment (all optional; everything is off until a class is enabled):

``BACKUP_ARCHIVE_DIR``            local archive root, the ``local`` location (default ``/app/backups/archive``)
``BACKUP_ARCHIVE_STATE_DIR``      private catalog directory (default ``/app/backups/archive-state``)
``BACKUP_JOURNAL_PATH``           shared change journal (default ``/app/backup-journal/config-changes.jsonl``)
``BACKUP_ARCHIVE_STATUS_FILE``    secret-free status for /healthz (default ``/app/backup-status/backup-archive.json``)
``BACKUP_ARCHIVE_PASSPHRASE_FILE`` encryption passphrase file (falls back to ``SCHEDULED_BACKUP_PASSPHRASE_FILE``)
``BACKUP_SCHEDULER_SOCKET``       Unix socket the admin sidecar calls (default ``/app/backup-api/scheduler.sock``)
``APP_GID``                       shared group for the status and journal directories

Policy (classes, retention, targets): ``backups`` in config.yaml plus the
``BACKUP_CONFIG_*`` / ``BACKUP_FULL_*`` / ``BACKUP_TARGETS_JSON`` overrides; see
``history_service/backup_archive/policy.py``.
"""

from __future__ import annotations

import logging
import os
import signal
import stat
import threading
from pathlib import Path
from typing import Any

from app.config_errors import ConfigurationError
from app.logging_config import configure_service_logging
from history_service.backup_archive.policy import BackupPolicy, load_backup_policy

logger = logging.getLogger(__name__)

DEFAULTS = {
    "BACKUP_ARCHIVE_DIR": "/app/backups/archive",
    "BACKUP_ARCHIVE_STATE_DIR": "/app/backups/archive-state",
    "BACKUP_JOURNAL_PATH": "/app/backup-journal/config-changes.jsonl",
    "BACKUP_ARCHIVE_STATUS_FILE": "/app/backup-status/backup-archive.json",
    "BACKUP_SCHEDULER_SOCKET": "/app/backup-api/scheduler.sock",
}


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip() or DEFAULTS.get(name, "")


def _app_gid() -> int:
    raw = (os.getenv("APP_GID") or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise ConfigurationError(["APP_GID in the environment must be a positive whole number."])
    return int(raw)


def build_scheduler(policy: BackupPolicy) -> Any:
    from app.config import _derive_runtime_layout_paths
    from history_service.backup_scheduler.service import (
        BackupScheduler,
        SchedulerPaths,
        config_group_keys,
        snapshot_config_files,
    )
    from history_service.config import get_history_settings
    from history_service.store import HistoryStore
    from history_service.system_backup import (
        DEFAULT_BACKUP_GROUP_KEYS,
        HISTORY_DB_KEY,
        SystemBackupService,
    )

    passphrase = (os.getenv("BACKUP_ARCHIVE_PASSPHRASE_FILE") or os.getenv("SCHEDULED_BACKUP_PASSPHRASE_FILE") or "").strip()
    if not passphrase and policy.any_enabled:
        raise ConfigurationError(
            ["BACKUP_ARCHIVE_PASSPHRASE_FILE in the environment is required when a backup class is enabled."]
        )
    status_file = Path(_env("BACKUP_ARCHIVE_STATUS_FILE"))
    full_status = (os.getenv("SCHEDULED_BACKUP_STATUS_FILE") or "").strip()
    history_settings = get_history_settings()
    backup_service = SystemBackupService(
        history_settings,
        HistoryStore(history_settings.sqlite_path, segment_catalog_path=history_settings.segment_catalog_path),
    )
    config_path = os.getenv("APP_CONFIG_PATH") or "/app/config/config.yaml"
    layout = _derive_runtime_layout_paths(config_path)
    # The slot-detail cache changes on every inventory poll; it is backed up but not
    # hashed, or every poll would look like a config change.
    hashed = {
        "config": Path(layout["config_file"]),
        "runtime_overrides": Path(layout["runtime_overrides_file"]),
        "profiles": Path(layout["profile_file"]),
        "mappings": Path(layout["mapping_file"]),
        "sas_fabric_aliases": Path(layout["sas_fabric_alias_file"]),
    }
    return BackupScheduler(
        policy,
        backup_service,
        SchedulerPaths(
            local_dir=Path(_env("BACKUP_ARCHIVE_DIR")),
            state_dir=Path(_env("BACKUP_ARCHIVE_STATE_DIR")),
            journal_path=Path(_env("BACKUP_JOURNAL_PATH")),
            status_file=status_file,
            # Unused while both classes are disabled (the API still serves the catalogue).
            passphrase_file=Path(passphrase or "/nonexistent-backup-passphrase"),
            runner_status_dir=status_file.parent,
            full_status_file=Path(full_status) if full_status else None,
            history_backup_dir=Path(history_settings.backup_dir),
            history_long_term_backup_dir=(
                Path(history_settings.long_term_backup_dir)
                if history_settings.long_term_backup_dir
                else None
            ),
            history_database_stem=Path(history_settings.sqlite_path).stem,
        ),
        app_gid=_app_gid(),
        config_groups=config_group_keys(DEFAULT_BACKUP_GROUP_KEYS, HISTORY_DB_KEY),
        full_groups=list(DEFAULT_BACKUP_GROUP_KEYS),
        snapshot_config=lambda: snapshot_config_files(hashed),
    )


def _prepare_socket(path: Path) -> None:
    directory = path.parent
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o007:
        raise ConfigurationError(
            [f"{directory} must be a directory without access for other users (mode 2750 or 2770)."]
        )
    if path.is_socket() or path.is_symlink():
        path.unlink()


def serve(scheduler: Any, socket_path: Path, stop: threading.Event) -> None:
    import uvicorn

    from history_service.backup_scheduler.api import build_app

    _prepare_socket(socket_path)
    config = uvicorn.Config(build_app(scheduler), uds=str(socket_path), log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    def loop() -> None:
        while not stop.is_set():
            try:
                scheduler.tick()
            except Exception as exc:  # noqa: BLE001 - keep scheduling after a failed tick
                logger.error("Backup scheduler tick failed with %s.", type(exc).__name__)
            stop.wait(scheduler.seconds_until_next_tick())
        server.should_exit = True

    worker = threading.Thread(target=loop, name="backup-scheduler", daemon=True)
    worker.start()
    try:
        # Group members (the admin sidecar) connect; nobody else.
        original = os.umask(0o007)
        try:
            server.run()
        finally:
            os.umask(original)
    finally:
        stop.set()
        worker.join(timeout=30)


def main() -> int:
    configure_service_logging(
        log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
        log_format=os.getenv("LOG_FORMAT", "text"),
        service_name="enclosure-backup-scheduler",
    )
    try:
        policy = load_backup_policy()
    except ConfigurationError as exc:
        for problem in exc.problems:
            logger.error("Configuration error: %s", problem)
        return 2
    if not policy.any_enabled:
        logger.info("Config and full backups are disabled; the backup scheduler is idle.")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        scheduler = build_scheduler(policy)
    except ConfigurationError as exc:
        for problem in exc.problems:
            logger.error("Configuration error: %s", problem)
        return 2
    except Exception as exc:  # noqa: BLE001 - bounded startup failure
        logger.error("Backup scheduler could not start (%s).", type(exc).__name__)
        return 1
    try:
        serve(scheduler, Path(_env("BACKUP_SCHEDULER_SOCKET")), stop)
    except ConfigurationError as exc:
        for problem in exc.problems:
            logger.error("Configuration error: %s", problem)
        return 2
    finally:
        scheduler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
