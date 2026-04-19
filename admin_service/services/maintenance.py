from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.domain import SystemBackupExportRequest


@dataclass(slots=True)
class MaintenanceOutcome:
    stopped_containers: list[str]
    restarted_containers: list[str]


class AdminMaintenanceService:
    def __init__(
        self,
        backup_service,
        runtime_service,
        *,
        clean_backup_targets: list[str] | tuple[str, ...],
    ) -> None:
        self.backup_service = backup_service
        self.runtime_service = runtime_service
        self.clean_backup_targets = tuple(clean_backup_targets)

    def export_bundle(
        self,
        payload: SystemBackupExportRequest,
        *,
        stop_services: bool = False,
        restart_services: bool = True,
    ) -> tuple[Any, MaintenanceOutcome]:
        stopped_containers: list[str] = []
        restarted_containers: list[str] = []
        if stop_services:
            stopped_containers = self.runtime_service.running_container_keys(self.clean_backup_targets)
            for key in stopped_containers:
                self.runtime_service.stop_container(key)
        try:
            artifact = self.backup_service.export_bundle(
                encrypt=payload.encrypt,
                passphrase=payload.passphrase,
                packaging=payload.packaging,
            )
        finally:
            if stop_services and restart_services:
                for key in stopped_containers:
                    self.runtime_service.start_container(key)
                    restarted_containers.append(key)
        return artifact, MaintenanceOutcome(stopped_containers, restarted_containers)

    def import_bundle(
        self,
        content: bytes,
        *,
        passphrase: str | None = None,
        stop_services: bool = False,
        restart_services: bool = True,
    ) -> tuple[dict[str, Any], MaintenanceOutcome]:
        stopped_containers: list[str] = []
        restarted_containers: list[str] = []
        if stop_services:
            stopped_containers = self.runtime_service.running_container_keys(self.clean_backup_targets)
            for key in stopped_containers:
                self.runtime_service.stop_container(key)
        try:
            result = self.backup_service.import_bundle(content, passphrase=passphrase)
        finally:
            if stop_services and restart_services:
                for key in stopped_containers:
                    self.runtime_service.start_container(key)
                    restarted_containers.append(key)
        return result, MaintenanceOutcome(stopped_containers, restarted_containers)
