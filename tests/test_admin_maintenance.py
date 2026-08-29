from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from admin_service.config import AdminSettings
from admin_service.services.maintenance import AdminMaintenanceService, MaintenanceStopError
from admin_service.services.runtime_control import (
    CONTAINER_CONTROL_RESPONSE_GRACE_SECONDS,
    DockerRuntimeError,
    DockerRuntimeService,
)
from app.models.domain import DebugBundleExportRequest, SystemBackupExportRequest


class FakeRuntimeService:
    """Scripted stand-in for DockerRuntimeService: which stops/starts fail is configurable."""

    def __init__(
        self,
        running: list[str],
        *,
        stop_failures: dict[str, str] | None = None,
        start_failures: dict[str, str] | None = None,
    ) -> None:
        self.running = list(running)
        self.stop_failures = dict(stop_failures or {})
        self.start_failures = dict(start_failures or {})
        self.calls: list[tuple[str, str]] = []
        self.status_calls = 0

    def running_container_keys(self, keys=None) -> list[str]:
        requested = set(keys or self.running)
        return [key for key in self.running if key in requested]

    def stop_container(self, key: str) -> None:
        self.calls.append(("stop", key))
        if key in self.stop_failures:
            raise DockerRuntimeError(self.stop_failures[key])
        self.running.remove(key)

    def start_container(self, key: str) -> None:
        self.calls.append(("start", key))
        if key in self.start_failures:
            raise DockerRuntimeError(self.start_failures[key])
        self.running.append(key)

    def status_payload(self) -> dict[str, Any]:
        self.status_calls += 1
        return {"available": True, "running": list(self.running)}


class FakeBackupService:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.export_calls = 0
        self.import_calls = 0
        self.debug_calls: list[dict[str, Any]] = []

    def export_bundle(self, **kwargs: Any) -> str:
        self.export_calls += 1
        if self.fail:
            raise self.fail
        return "artifact"

    def export_debug_bundle(self, **kwargs: Any) -> str:
        self.debug_calls.append(kwargs)
        if self.fail:
            raise self.fail
        return "debug-artifact"

    def import_bundle(self, content: bytes, *, passphrase: str | None = None) -> dict[str, Any]:
        self.import_calls += 1
        if self.fail:
            raise self.fail
        return {"ok": True}


def build_service(runtime: FakeRuntimeService, backup: FakeBackupService) -> AdminMaintenanceService:
    return AdminMaintenanceService(backup, runtime, clean_backup_targets=("ui", "history"))


class MaintenanceQuiesceTests(unittest.TestCase):
    def test_happy_path_stops_operates_and_restarts_every_target(self) -> None:
        runtime = FakeRuntimeService(["ui", "history", "admin"])
        backup = FakeBackupService()

        result, outcome = build_service(runtime, backup).import_bundle(b"bundle", stop_services=True)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(outcome.stopped_containers, ["ui", "history"])
        self.assertEqual(outcome.restarted_containers, ["ui", "history"])
        self.assertEqual(outcome.restart_failures, {})
        self.assertEqual(runtime.calls, [("stop", "ui"), ("stop", "history"), ("start", "ui"), ("start", "history")])
        self.assertEqual(sorted(runtime.running), ["admin", "history", "ui"])

    def test_stop_failure_fails_closed_and_restarts_what_was_already_stopped(self) -> None:
        runtime = FakeRuntimeService(["ui", "history"], stop_failures={"history": "HTTP 500: daemon busy"})
        backup = FakeBackupService()

        with self.assertRaises(MaintenanceStopError) as raised:
            build_service(runtime, backup).import_bundle(b"bundle", stop_services=True)

        self.assertEqual(backup.import_calls, 0, "import must not run against a partially quiesced stack")
        self.assertIsInstance(raised.exception, DockerRuntimeError)
        self.assertEqual(raised.exception.failed_key, "history")
        self.assertEqual(raised.exception.stopped_containers, ["ui"])
        self.assertEqual(raised.exception.restarted_containers, ["ui"])
        self.assertEqual(raised.exception.restart_failures, {})
        self.assertIn("history", str(raised.exception))
        self.assertIn("Restarted: ui", str(raised.exception))
        self.assertEqual(runtime.calls, [("stop", "ui"), ("stop", "history"), ("start", "ui")])
        self.assertEqual(sorted(runtime.running), ["history", "ui"])

    def test_stop_failure_reports_containers_it_could_not_bring_back(self) -> None:
        runtime = FakeRuntimeService(
            ["ui", "history"],
            stop_failures={"history": "HTTP 500: daemon busy"},
            start_failures={"ui": "HTTP 404: no such container"},
        )
        backup = FakeBackupService()

        with self.assertRaises(MaintenanceStopError) as raised:
            build_service(runtime, backup).export_bundle(
                SystemBackupExportRequest(), stop_services=True, restart_services=True
            )

        self.assertEqual(raised.exception.restarted_containers, [])
        self.assertEqual(raised.exception.restart_failures, {"ui": "HTTP 404: no such container"})
        self.assertIn("Still stopped: ui", str(raised.exception))
        self.assertEqual(backup.export_calls, 0)

    def test_stop_failure_without_restart_leaves_stopped_containers_down_but_reports_them(self) -> None:
        runtime = FakeRuntimeService(["ui", "history"], stop_failures={"history": "boom"})

        with self.assertRaises(MaintenanceStopError) as raised:
            build_service(runtime, FakeBackupService()).import_bundle(
                b"bundle", stop_services=True, restart_services=False
            )

        self.assertEqual(raised.exception.stopped_containers, ["ui"])
        self.assertEqual(raised.exception.restarted_containers, [])
        self.assertEqual(runtime.calls, [("stop", "ui"), ("stop", "history")])

    def test_restart_loop_keeps_going_past_a_failed_start_and_reports_it(self) -> None:
        runtime = FakeRuntimeService(["ui", "history"], start_failures={"ui": "HTTP 500: cannot start"})
        backup = FakeBackupService()

        result, outcome = build_service(runtime, backup).import_bundle(b"bundle", stop_services=True)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(outcome.stopped_containers, ["ui", "history"])
        self.assertEqual(outcome.restarted_containers, ["history"], "history must still be started after ui fails")
        self.assertEqual(outcome.restart_failures, {"ui": "HTTP 500: cannot start"})
        self.assertEqual(runtime.calls, [("stop", "ui"), ("stop", "history"), ("start", "ui"), ("start", "history")])
        self.assertEqual(runtime.running, ["history"])

    def test_operation_failure_still_restarts_every_stopped_container(self) -> None:
        runtime = FakeRuntimeService(["ui", "history"], start_failures={"ui": "HTTP 500: cannot start"})
        backup = FakeBackupService(fail=ValueError("Bundle manifest is invalid."))

        with self.assertRaises(ValueError):
            build_service(runtime, backup).import_bundle(b"bundle", stop_services=True)

        self.assertEqual(runtime.calls, [("stop", "ui"), ("stop", "history"), ("start", "ui"), ("start", "history")])
        self.assertEqual(runtime.running, ["history"])

    def test_restart_services_false_leaves_targets_stopped(self) -> None:
        runtime = FakeRuntimeService(["ui", "history"])

        _, outcome = build_service(runtime, FakeBackupService()).export_bundle(
            SystemBackupExportRequest(), stop_services=True, restart_services=False
        )

        self.assertEqual(outcome.stopped_containers, ["ui", "history"])
        self.assertEqual(outcome.restarted_containers, [])
        self.assertEqual(outcome.restart_failures, {})
        self.assertEqual(runtime.running, [])

    def test_stop_services_false_never_touches_the_runtime(self) -> None:
        runtime = FakeRuntimeService(["ui", "history"])

        _, outcome = build_service(runtime, FakeBackupService()).export_bundle(SystemBackupExportRequest())

        self.assertEqual(outcome.stopped_containers, [])
        self.assertEqual(outcome.restarted_containers, [])
        self.assertEqual(runtime.calls, [])

    def test_debug_export_records_stopped_containers_and_runtime_snapshots(self) -> None:
        runtime = FakeRuntimeService(["ui", "history"])
        backup = FakeBackupService()

        _, outcome = build_service(runtime, backup).export_debug_bundle(DebugBundleExportRequest())

        self.assertEqual(outcome.stopped_containers, ["ui", "history"])
        self.assertEqual(outcome.restarted_containers, ["ui", "history"])
        self.assertEqual(len(backup.debug_calls), 1)
        maintenance_payload = backup.debug_calls[0]["maintenance_payload"]
        self.assertEqual(maintenance_payload["stopped_containers"], ["ui", "history"])
        runtime_payload = backup.debug_calls[0]["runtime_payload"]
        self.assertEqual(runtime_payload["before_stop"]["running"], ["ui", "history"])
        self.assertEqual(runtime_payload["after_stop"]["running"], [])


class DockerControlTimeoutTests(unittest.TestCase):
    def _service(self, grace: int) -> DockerRuntimeService:
        settings = AdminSettings(container_control_timeout_seconds=grace, docker_socket_path="/nonexistent.sock")
        return DockerRuntimeService(settings)

    def test_stop_and_restart_wait_longer_than_the_stop_grace_period(self) -> None:
        service = self._service(30)
        seen: list[tuple[str, str, int | None]] = []

        def fake_request(method: str, path: str, body: bytes | None = None, *, timeout: int | None = None) -> bytes:
            seen.append((method, path, timeout))
            return b""

        with patch.object(service, "_request", fake_request):
            service.stop_container("ui")
            service.restart_container("history")
            service.start_container("ui")

        stop_method, stop_path, stop_timeout = seen[0]
        self.assertEqual(stop_method, "POST")
        self.assertIn("/stop?t=30", stop_path)
        self.assertEqual(stop_timeout, 30 + CONTAINER_CONTROL_RESPONSE_GRACE_SECONDS)

        restart_method, restart_path, restart_timeout = seen[1]
        self.assertIn("/restart?t=30", restart_path)
        self.assertEqual(restart_timeout, 30 + CONTAINER_CONTROL_RESPONSE_GRACE_SECONDS)

        start_method, start_path, start_timeout = seen[2]
        self.assertTrue(start_path.endswith("/start"))
        self.assertIsNone(start_timeout, "start has no grace period and keeps the default control timeout")

    def test_request_uses_explicit_timeout_for_the_socket_connection(self) -> None:
        service = self._service(30)
        created: list[int] = []

        class FakeConnection:
            def __init__(self, socket_path: str, timeout: int = 5) -> None:
                created.append(timeout)

            def request(self, *args: Any, **kwargs: Any) -> None:
                raise OSError("no daemon")

            def close(self) -> None:
                return None

        with patch("admin_service.services.runtime_control.Path.exists", return_value=True):
            with patch("admin_service.services.runtime_control.UnixSocketHTTPConnection", FakeConnection):
                with self.assertRaises(DockerRuntimeError):
                    service._request("POST", "/containers/x/stop?t=30", timeout=45)
                with self.assertRaises(DockerRuntimeError):
                    service._request("GET", "/containers/json?all=1")

        self.assertEqual(created, [45, 30])


if __name__ == "__main__":
    unittest.main()
