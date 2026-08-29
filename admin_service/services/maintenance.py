from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from admin_service.services.runtime_control import DockerRuntimeError
from app.models.domain import DebugBundleExportRequest, SystemBackupExportRequest

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MaintenanceOutcome:
    stopped_containers: list[str]
    restarted_containers: list[str]
    # Containers that were stopped for the operation but could not be started again.
    # The operation itself still completed; callers must surface these so the operator
    # knows what is still down.
    restart_failures: dict[str, str] = field(default_factory=dict)


class MaintenanceStopError(DockerRuntimeError):
    """A requested service stop failed, so the maintenance operation was not attempted.

    Carries the containers that were already stopped and (best effort) restarted so the
    operator can see the runtime state the sidecar left behind.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_key: str,
        stopped_containers: list[str],
        restarted_containers: list[str],
        restart_failures: dict[str, str],
    ) -> None:
        super().__init__(message)
        self.failed_key = failed_key
        self.stopped_containers = list(stopped_containers)
        self.restarted_containers = list(restarted_containers)
        self.restart_failures = dict(restart_failures)


class MaintenanceOperationError(DockerRuntimeError):
    """The maintenance operation failed and one or more stopped services stayed down."""

    def __init__(
        self,
        operation_error: Exception,
        *,
        stopped_containers: list[str],
        restarted_containers: list[str],
        restart_failures: dict[str, str],
    ) -> None:
        failure_detail = ", ".join(
            f"{key} ({reason})" for key, reason in restart_failures.items()
        )
        super().__init__(
            f"{operation_error} Maintenance cleanup could not restart: {failure_detail}."
        )
        self.operation_error = operation_error
        self.stopped_containers = list(stopped_containers)
        self.restarted_containers = list(restarted_containers)
        self.restart_failures = dict(restart_failures)


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

    # ------------------------------------------------------------------ helpers

    def _stop_targets(self, *, restart_on_failure: bool) -> list[str]:
        """Stop every running clean-backup target, failing closed.

        If any stop fails, no further stops are attempted, the containers that were
        already stopped are (best effort) started again, and ``MaintenanceStopError`` is
        raised so the caller never runs the operation against a partially quiesced stack.
        """

        stopped: list[str] = []
        for key in self.runtime_service.running_container_keys(self.clean_backup_targets):
            try:
                self.runtime_service.stop_container(key)
            except DockerRuntimeError as exc:
                logger.warning("Failed to stop container %s for maintenance: %s", key, exc)
                restarted: list[str] = []
                restart_failures: dict[str, str] = {}
                if restart_on_failure:
                    restarted, restart_failures = self._start_targets(stopped)
                detail = f"Failed to stop container '{key}' before maintenance: {exc}"
                if stopped:
                    detail += f" Already stopped: {', '.join(stopped)}."
                    detail += f" Restarted: {', '.join(restarted) or 'none'}."
                if restart_failures:
                    detail += " Still stopped: " + ", ".join(
                        f"{failed_key} ({reason})" for failed_key, reason in restart_failures.items()
                    ) + "."
                raise MaintenanceStopError(
                    detail,
                    failed_key=key,
                    stopped_containers=stopped,
                    restarted_containers=restarted,
                    restart_failures=restart_failures,
                ) from exc
            stopped.append(key)
        return stopped

    def _start_targets(self, keys: list[str]) -> tuple[list[str], dict[str, str]]:
        """Start every container in ``keys``; keep going past failures and report them."""

        restarted: list[str] = []
        failures: dict[str, str] = {}
        for key in keys:
            try:
                self.runtime_service.start_container(key)
            except DockerRuntimeError as exc:
                logger.warning("Failed to restart container %s after maintenance: %s", key, exc)
                failures[key] = str(exc)
                continue
            restarted.append(key)
        return restarted, failures

    def _run_with_quiesced_services(
        self,
        operation: Callable[[list[str]], Any],
        *,
        stop_services: bool,
        restart_services: bool,
    ) -> tuple[Any, MaintenanceOutcome]:
        stopped_containers: list[str] = []
        restarted_containers: list[str] = []
        restart_failures: dict[str, str] = {}
        if stop_services:
            stopped_containers = self._stop_targets(restart_on_failure=restart_services)
        try:
            result = operation(stopped_containers)
        except Exception as operation_error:
            if stop_services and restart_services:
                restarted_containers, restart_failures = self._start_targets(stopped_containers)
            if restart_failures:
                raise MaintenanceOperationError(
                    operation_error,
                    stopped_containers=stopped_containers,
                    restarted_containers=restarted_containers,
                    restart_failures=restart_failures,
                ) from operation_error
            raise
        if stop_services and restart_services:
            restarted_containers, restart_failures = self._start_targets(stopped_containers)
        return result, MaintenanceOutcome(stopped_containers, restarted_containers, restart_failures)

    # --------------------------------------------------------------- operations

    def export_bundle(
        self,
        payload: SystemBackupExportRequest,
        *,
        stop_services: bool = False,
        restart_services: bool = True,
    ) -> tuple[Any, MaintenanceOutcome]:
        def operation(_stopped: list[str]) -> Any:
            return self.backup_service.export_bundle_to_file(
                encrypt=payload.encrypt,
                passphrase=payload.passphrase,
                packaging=payload.packaging,
                included_paths=payload.included_paths,
            )

        return self._run_with_quiesced_services(
            operation,
            stop_services=stop_services,
            restart_services=restart_services,
        )

    def export_debug_bundle(
        self,
        payload: DebugBundleExportRequest,
        *,
        stop_services: bool = True,
        restart_services: bool = True,
    ) -> tuple[Any, MaintenanceOutcome]:
        runtime_before = self.runtime_service.status_payload()

        def operation(stopped_containers: list[str]) -> Any:
            runtime_after_stop = self.runtime_service.status_payload()
            return self.backup_service.export_debug_bundle_to_file(
                encrypt=payload.encrypt,
                passphrase=payload.passphrase,
                packaging=payload.packaging,
                included_paths=payload.included_paths,
                scrub_secrets=payload.scrub_secrets,
                scrub_disk_identifiers=payload.scrub_disk_identifiers,
                runtime_payload={
                    "before_stop": runtime_before,
                    "after_stop": runtime_after_stop,
                },
                maintenance_payload={
                    "stop_services": stop_services,
                    "restart_services": restart_services,
                    "stopped_containers": list(stopped_containers),
                },
            )

        return self._run_with_quiesced_services(
            operation,
            stop_services=stop_services,
            restart_services=restart_services,
        )

    def import_bundle(
        self,
        content: bytes,
        *,
        passphrase: str | None = None,
        stop_services: bool = False,
        restart_services: bool = True,
    ) -> tuple[dict[str, Any], MaintenanceOutcome]:
        def operation(_stopped: list[str]) -> dict[str, Any]:
            return self.backup_service.import_bundle(content, passphrase=passphrase)

        return self._run_with_quiesced_services(
            operation,
            stop_services=stop_services,
            restart_services=restart_services,
        )
