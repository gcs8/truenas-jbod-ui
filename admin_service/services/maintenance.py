from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
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
    final_running_containers: list[str] = field(default_factory=list)


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
        final_running_containers: list[str],
    ) -> None:
        super().__init__(message)
        self.failed_key = failed_key
        self.stopped_containers = list(stopped_containers)
        self.restarted_containers = list(restarted_containers)
        self.restart_failures = dict(restart_failures)
        self.final_running_containers = list(final_running_containers)


class MaintenanceOperationError(DockerRuntimeError):
    """The maintenance operation failed and one or more stopped services stayed down."""

    def __init__(
        self,
        operation_error: Exception,
        *,
        stopped_containers: list[str],
        restarted_containers: list[str],
        restart_failures: dict[str, str],
        final_running_containers: list[str],
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
        self.final_running_containers = list(final_running_containers)


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

    @staticmethod
    def _stage_archive_snapshot(archive_path: Path) -> tuple[Path, Path]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(archive_path, flags)
        except OSError as exc:
            raise ValueError("Backup bundle archive could not be opened.") from exc
        try:
            workspace = Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-admitted-import-"))
            snapshot = workspace / "bundle.archive"
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("Backup bundle archive exceeds its size or type limits.")
                copied = 0
                with os.fdopen(os.dup(descriptor), "rb", closefd=True) as source, snapshot.open(
                    "xb"
                ) as destination:
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > metadata.st_size:
                            raise ValueError("Backup bundle archive changed while being staged.")
                        destination.write(chunk)
                    if copied != metadata.st_size:
                        raise ValueError("Backup bundle archive changed while being staged.")
                    destination.flush()
                    os.fsync(destination.fileno())
                snapshot.chmod(0o400)
                workspace.chmod(0o500)
                return snapshot, workspace
            except Exception:
                shutil.rmtree(workspace, ignore_errors=True)
                raise
        finally:
            os.close(descriptor)

    def _stop_targets(
        self,
        initially_running: list[str],
        *,
        restart_on_failure: bool,
    ) -> list[str]:
        """Stop every running clean-backup target, failing closed."""

        stopped: list[str] = []
        for key in initially_running:
            try:
                self.runtime_service.stop_container(key)
            except DockerRuntimeError as exc:
                logger.warning("Failed to stop container %s for maintenance: %s", key, exc)
                restarted: list[str] = []
                restart_failures: dict[str, str] = {}
                final_running: list[str] = []
                try:
                    observed_running: set[str] | None = set(
                        self.runtime_service.running_container_keys(initially_running)
                    )
                except DockerRuntimeError:
                    observed_running = None
                if observed_running is not None and key not in observed_running:
                    stopped.append(key)
                if restart_on_failure:
                    restarted, restart_failures, final_running = self._restore_initial_running(
                        initially_running,
                        stopped,
                    )
                elif observed_running is not None:
                    final_running = [
                        item for item in initially_running if item in observed_running
                    ]
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
                    final_running_containers=final_running,
                ) from exc
            stopped.append(key)
        return stopped

    def _restore_initial_running(
        self,
        initially_running: list[str],
        stopped: list[str],
    ) -> tuple[list[str], dict[str, str], list[str]]:
        """Best-effort restore with separate transition and final-state evidence."""

        start_errors: dict[str, str] = {}
        start_attempts: set[str] = set()
        try:
            currently_running = set(
                self.runtime_service.running_container_keys(initially_running)
            )
        except DockerRuntimeError:
            currently_running = set()
        for key in initially_running:
            if key in currently_running:
                continue
            start_attempts.add(key)
            try:
                self.runtime_service.start_container(key)
            except DockerRuntimeError as exc:
                logger.warning("Failed to restart container %s after maintenance: %s", key, exc)
                start_errors[key] = str(exc)
        try:
            final_running = set(
                self.runtime_service.running_container_keys(initially_running)
            )
        except DockerRuntimeError as exc:
            final_running = set()
            start_errors = {key: str(exc) for key in initially_running}
        restarted = [
            key
            for key in stopped
            if key in start_attempts and key in final_running
        ]
        failures = {
            key: start_errors.get(key, "not observed running after restart")
            for key in initially_running
            if key not in final_running
        }
        final_state = [key for key in initially_running if key in final_running]
        return restarted, failures, final_state

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
        final_running_containers: list[str] = []
        initially_running: list[str] = []
        if stop_services:
            initially_running = self.runtime_service.running_container_keys(
                self.clean_backup_targets
            )
            stopped_containers = self._stop_targets(
                initially_running,
                restart_on_failure=restart_services,
            )
        try:
            result = operation(stopped_containers)
        except Exception as operation_error:
            if stop_services and restart_services:
                (
                    restarted_containers,
                    restart_failures,
                    final_running_containers,
                ) = self._restore_initial_running(
                    initially_running,
                    stopped_containers,
                )
            if restart_failures:
                raise MaintenanceOperationError(
                    operation_error,
                    stopped_containers=stopped_containers,
                    restarted_containers=restarted_containers,
                    restart_failures=restart_failures,
                    final_running_containers=final_running_containers,
                ) from operation_error
            raise
        if stop_services and restart_services:
            (
                restarted_containers,
                restart_failures,
                final_running_containers,
            ) = self._restore_initial_running(
                initially_running,
                stopped_containers,
            )
        return result, MaintenanceOutcome(
            stopped_containers,
            restarted_containers,
            restart_failures,
            final_running_containers,
        )

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

    def import_bundle_from_file(
        self,
        archive_path: Path,
        *,
        passphrase: str | None = None,
        expected_encrypted: bool | None = None,
        stop_services: bool = False,
        restart_services: bool = True,
        admission_callback: Callable[[str, str], None] | None = None,
    ) -> tuple[dict[str, Any], MaintenanceOutcome]:
        snapshot, workspace = self._stage_archive_snapshot(archive_path)
        primary_error: Exception | None = None
        try:
            preflight = getattr(
                self.backup_service,
                "preflight_import_bundle_file",
                self.backup_service.inspect_bundle_file,
            )
            preflight(
                snapshot,
                passphrase=passphrase,
                expected_encrypted=expected_encrypted,
                identity_callback=admission_callback,
            )

            def operation(_stopped: list[str]) -> dict[str, Any]:
                return self.backup_service.import_bundle_from_file(
                    snapshot,
                    passphrase=passphrase,
                    expected_encrypted=expected_encrypted,
                )

            return self._run_with_quiesced_services(
                operation,
                stop_services=stop_services,
                restart_services=restart_services,
            )
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            try:
                workspace.chmod(0o700)
                # The staged copy is read-only; make it writable again so the
                # removal also works where a read-only file cannot be unlinked.
                snapshot.chmod(0o600)
                shutil.rmtree(workspace)
            except Exception:
                if primary_error is not None:
                    raise RuntimeError(
                        f"{primary_error} Backup admission snapshot cleanup also failed."
                    ) from primary_error
                raise RuntimeError("Backup admission snapshot cleanup failed.")
