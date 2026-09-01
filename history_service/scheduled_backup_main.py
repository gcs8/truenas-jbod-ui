from __future__ import annotations

import logging
import os

from app.logging_config import configure_service_logging
from history_service.config import get_history_settings
from history_service.scheduled_backup import (
    ScheduledBackupRunner,
    ScheduledBackupSettings,
)
from history_service.store import HistoryStore
from history_service.system_backup import SystemBackupService

logger = logging.getLogger(__name__)


def main() -> int:
    configure_service_logging(
        log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
        log_format=os.getenv("LOG_FORMAT", "text"),
        service_name="enclosure-backup",
    )
    try:
        settings = ScheduledBackupSettings.from_environment()
        if not settings.enabled:
            logger.info("Scheduled state backup is disabled.")
            return 0
        assert settings.destination_dir is not None
        assert settings.status_file is not None
        assert settings.passphrase_file is not None
        assert settings.app_gid is not None
        history_settings = get_history_settings()
        backup_service = SystemBackupService(
            history_settings,
            HistoryStore(
                history_settings.sqlite_path,
                segment_catalog_path=history_settings.segment_catalog_path,
            ),
        )
        runner = ScheduledBackupRunner(
            backup_service,
            destination_dir=settings.destination_dir,
            status_file=settings.status_file,
            passphrase_file=settings.passphrase_file,
            included_groups=settings.included_groups,
            retention_count=settings.retention_count,
            app_gid=settings.app_gid,
        )
        result = runner.run_once()
        logger.info(
            "Scheduled state backup completed: size_bytes=%d retention_removed=%d",
            int(result.get("last_size_bytes") or 0),
            int(result.get("last_retention_removed") or 0),
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI must return a bounded failure status
        logger.error("Scheduled state backup failed with %s.", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
