from __future__ import annotations

import http.client
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from admin_service.config import AdminSettings
from admin_service.services.maintenance import (
    AdminMaintenanceService,
    MaintenanceOperationError,
    MaintenanceStopError,
)
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

    def export_bundle_to_file(self, **kwargs: Any) -> str:
        return self.export_bundle(**kwargs)

    def export_debug_bundle(self, **kwargs: Any) -> str:
        self.debug_calls.append(kwargs)
        if self.fail:
            raise self.fail
        return "debug-artifact"

    def export_debug_bundle_to_file(self, **kwargs: Any) -> str:
        return self.export_debug_bundle(**kwargs)

    def import_bundle(self, content: bytes, *, passphrase: str | None = None) -> dict[str, Any]:
        self.import_calls += 1
        if self.fail:
            raise self.fail
        return {"ok": True}


def build_service(runtime: FakeRuntimeService, backup: FakeBackupService) -> AdminMaintenanceService:
    return AdminMaintenanceService(backup, runtime, clean_backup_targets=("ui", "history"))


class MaintenanceQuiesceTests(unittest.TestCase):
    def test_archive_snapshot_closes_descriptor_when_workspace_creation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "synthetic.archive"
            archive_path.write_bytes(b"synthetic")
            descriptors: list[int] = []
            real_open = os.open

            def capture_open(path: Path, flags: int) -> int:
                descriptor = real_open(path, flags)
                descriptors.append(descriptor)
                return descriptor

            with (
                patch(
                    "admin_service.services.maintenance.os.open",
                    side_effect=capture_open,
                ),
                patch(
                    "admin_service.services.maintenance.tempfile.mkdtemp",
                    side_effect=OSError("synthetic workspace failure"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "synthetic workspace failure"):
                    AdminMaintenanceService._stage_archive_snapshot(archive_path)

            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])

    def test_file_import_activates_the_exact_snapshot_that_preflight_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "synthetic.archive"
            archive_path.write_bytes(b"inspected-a")
            observed: dict[str, object] = {}
            runtime = FakeRuntimeService(["ui", "history"])
            original_stop = runtime.stop_container

            def stop_container(key: str) -> None:
                if key == "ui":
                    preflight_path = observed["preflight_path"]
                    assert isinstance(preflight_path, Path)
                    replacement = preflight_path.with_suffix(".replacement")
                    try:
                        replacement.write_bytes(b"uninspected-b")
                        replacement.replace(preflight_path)
                    except OSError:
                        observed["replacement_rejected"] = True
                        replacement.unlink(missing_ok=True)
                original_stop(key)

            def inspect(path: Path, **_kwargs: object) -> bytes:
                observed["preflight_path"] = Path(path)
                content = Path(path).read_bytes()
                observed["preflight"] = content
                return content

            runtime.stop_container = stop_container  # type: ignore[method-assign]
            backup = FakeBackupService()
            backup.inspect_bundle_file = inspect  # type: ignore[attr-defined]
            backup.import_bundle_from_file = lambda path, **_kwargs: (  # type: ignore[attr-defined]
                observed.setdefault("activated", Path(path).read_bytes()) or {"ok": True}
            )

            build_service(runtime, backup).import_bundle_from_file(
                archive_path,
                stop_services=True,
                restart_services=False,
            )

            self.assertEqual(observed["preflight"], b"inspected-a")
            self.assertEqual(observed["activated"], b"inspected-a")
            self.assertIs(observed.get("replacement_rejected"), True)

    def test_file_import_preflights_before_stopping_services(self) -> None:
        events: list[str] = []
        runtime = FakeRuntimeService(["ui", "history"])
        original_stop = runtime.stop_container

        def stop_container(key: str) -> None:
            events.append(f"stop:{key}")
            original_stop(key)

        runtime.stop_container = stop_container  # type: ignore[method-assign]
        backup = FakeBackupService()
        backup.inspect_bundle_file = lambda *_args, **_kwargs: events.append("preflight")  # type: ignore[attr-defined]
        backup.import_bundle_from_file = lambda *_args, **_kwargs: (  # type: ignore[attr-defined]
            events.append("import") or {"ok": True}
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "synthetic.archive"
            archive_path.write_bytes(b"synthetic")
            build_service(runtime, backup).import_bundle_from_file(
                archive_path,
                stop_services=True,
            )

        self.assertLess(events.index("preflight"), events.index("stop:ui"))
        self.assertLess(events.index("stop:history"), events.index("import"))

    def test_stop_error_after_effect_recovers_complete_initial_running_state(self) -> None:
        class EffectThenErrorRuntime(FakeRuntimeService):
            def stop_container(self, key: str) -> None:
                super().stop_container(key)
                if key == "ui":
                    raise DockerRuntimeError("response lost after stop")

        runtime = EffectThenErrorRuntime(["ui", "history"])
        backup = FakeBackupService()
        backup.inspect_bundle_file = lambda *_args, **_kwargs: {"ok": True}  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "synthetic.archive"
            archive_path.write_bytes(b"synthetic")
            with self.assertRaises(MaintenanceStopError) as raised:
                build_service(runtime, backup).import_bundle_from_file(
                    archive_path,
                    stop_services=True,
                )

        self.assertEqual(sorted(runtime.running), ["history", "ui"])
        self.assertEqual(raised.exception.stopped_containers, ["ui"])
        self.assertEqual(raised.exception.restarted_containers, ["ui"])
        self.assertEqual(raised.exception.restart_failures, {})

    def test_start_success_without_observed_running_state_is_not_restart_success(self) -> None:
        class UnobservedStartRuntime(FakeRuntimeService):
            def start_container(self, key: str) -> None:
                self.calls.append(("start", key))

        runtime = UnobservedStartRuntime(["ui", "history"])
        backup = FakeBackupService()

        _result, outcome = build_service(runtime, backup).import_bundle(
            b"bundle",
            stop_services=True,
        )

        self.assertEqual(outcome.restarted_containers, [])
        self.assertEqual(set(outcome.restart_failures), {"ui", "history"})

    def test_start_error_after_effect_is_reconciled_as_observed_restart_success(self) -> None:
        class EffectThenErrorRuntime(FakeRuntimeService):
            def start_container(self, key: str) -> None:
                super().start_container(key)
                raise DockerRuntimeError("response lost after start")

        runtime = EffectThenErrorRuntime(["ui", "history"])
        backup = FakeBackupService()

        _result, outcome = build_service(runtime, backup).import_bundle(
            b"bundle",
            stop_services=True,
        )

        self.assertEqual(outcome.restarted_containers, ["ui", "history"])
        self.assertEqual(outcome.restart_failures, {})
        self.assertEqual(sorted(runtime.running), ["history", "ui"])

    def test_stop_error_before_effect_after_observation_failure_reports_no_false_transitions(self) -> None:
        class AmbiguousRuntime(FakeRuntimeService):
            def __init__(self) -> None:
                super().__init__(["ui", "history"])
                self.observation_calls = 0

            def stop_container(self, key: str) -> None:
                self.calls.append(("stop", key))
                raise DockerRuntimeError("stop response unavailable")

            def running_container_keys(self, keys=None) -> list[str]:
                self.observation_calls += 1
                if self.observation_calls == 2:
                    raise DockerRuntimeError("observation unavailable")
                return super().running_container_keys(keys)

            def start_container(self, key: str) -> None:
                self.calls.append(("start", key))
                if key not in self.running:
                    self.running.append(key)

        runtime = AmbiguousRuntime()

        with self.assertRaises(MaintenanceStopError) as raised:
            build_service(runtime, FakeBackupService()).import_bundle(
                b"bundle",
                stop_services=True,
            )

        self.assertEqual(raised.exception.stopped_containers, [])
        self.assertEqual(raised.exception.restarted_containers, [])
        self.assertEqual(raised.exception.restart_failures, {})
        self.assertEqual(raised.exception.final_running_containers, ["ui", "history"])
        self.assertEqual(runtime.calls, [("stop", "ui")])

    def test_partial_recovery_reports_only_confirmed_restart_and_final_state(self) -> None:
        runtime = FakeRuntimeService(
            ["ui", "history"],
            stop_failures={"history": "stop failed before effect"},
            start_failures={"ui": "start failed"},
        )

        with self.assertRaises(MaintenanceStopError) as raised:
            build_service(runtime, FakeBackupService()).import_bundle(
                b"bundle",
                stop_services=True,
            )

        self.assertEqual(raised.exception.stopped_containers, ["ui"])
        self.assertEqual(raised.exception.restarted_containers, [])
        self.assertEqual(raised.exception.restart_failures, {"ui": "start failed"})
        self.assertEqual(raised.exception.final_running_containers, ["history"])

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

        with self.assertRaises(MaintenanceOperationError) as raised:
            build_service(runtime, backup).import_bundle(b"bundle", stop_services=True)

        self.assertEqual(runtime.calls, [("stop", "ui"), ("stop", "history"), ("start", "ui"), ("start", "history")])
        self.assertEqual(runtime.running, ["history"])
        self.assertIsInstance(raised.exception.operation_error, ValueError)
        self.assertEqual(raised.exception.stopped_containers, ["ui", "history"])
        self.assertEqual(raised.exception.restarted_containers, ["history"])
        self.assertEqual(raised.exception.restart_failures, {"ui": "HTTP 500: cannot start"})
        self.assertIn("Bundle manifest is invalid", str(raised.exception))
        self.assertIn("ui", str(raised.exception))

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

    def test_start_container_keeps_restart_required_when_docker_returns_304(self) -> None:
        service = self._service(30)
        service.mark_restart_required(("ui",))

        class FakeResponse:
            status = 304

            def read(self) -> bytes:
                return b""

        class FakeConnection:
            def __init__(self, socket_path: str, timeout: int = 5) -> None:
                pass

            def request(self, *args: Any, **kwargs: Any) -> None:
                pass

            def getresponse(self) -> FakeResponse:
                return FakeResponse()

            def close(self) -> None:
                pass

        with (
            patch("admin_service.services.runtime_control.Path.exists", return_value=True),
            patch(
                "admin_service.services.runtime_control.UnixSocketHTTPConnection",
                FakeConnection,
            ),
            self.assertRaises(DockerRuntimeError),
        ):
            service.start_container("ui")

        self.assertEqual(service.pending_restart_keys, {"ui"})

    def test_request_normalizes_http_client_exceptions(self) -> None:
        service = self._service(30)
        errors = (
            http.client.BadStatusLine("malformed status"),
            http.client.IncompleteRead(b"partial", 8),
        )

        for error in errors:
            with self.subTest(error=type(error).__name__):
                connection = MagicMock()
                if isinstance(error, http.client.IncompleteRead):
                    response = MagicMock()
                    response.status = 200
                    response.read.side_effect = error
                    connection.getresponse.return_value = response
                else:
                    connection.getresponse.side_effect = error

                with (
                    patch(
                        "admin_service.services.runtime_control.Path.exists",
                        return_value=True,
                    ),
                    patch(
                        "admin_service.services.runtime_control.UnixSocketHTTPConnection",
                        return_value=connection,
                    ),
                    self.assertRaises(DockerRuntimeError) as raised,
                ):
                    service._request("GET", "/containers/json?all=1")

                self.assertIn("Unable to talk to the Docker runtime", str(raised.exception))
                self.assertIs(raised.exception.__cause__, error)

    def test_running_container_keys_fails_when_runtime_status_is_unavailable(self) -> None:
        service = self._service(30)

        with self.assertRaisesRegex(DockerRuntimeError, "not mounted"):
            service.running_container_keys(("ui", "history"))


if __name__ == "__main__":
    unittest.main()
