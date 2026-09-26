from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Request
from fastapi.routing import APIRoute

# Must precede admin_service.main, which builds its app at import time.
import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
from app import main as app_main
from app import route_support as app_route_support
from app import routes as app_routes
from app.config import PathConfig, Settings, SSHConfig, SystemConfig
from app.models.domain import (
    InventorySnapshot,
    SourceStatus,
    StorageViewRuntimePayload,
    SystemOption,
)
from app.services.storage_writability import probe_writable_directories

REVISION = "a" * 64
CHOWN_SENTENCE = (
    "Cannot write to /app/data (owned by uid 0, running as uid 10001). "
    "On the Docker host run: sudo chown -R 10001:10001 ./data"
)


def _route(path: str, method: str = "GET") -> APIRoute:
    return next(
        route
        for route in app_main.app.routes
        if isinstance(route, APIRoute)
        and getattr(route, "path", None) == path
        and method in (getattr(route, "methods", None) or set())
    )


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "root_path": "",
            "app": app_main.app,
        }
    )


def _snapshot(*, api_ok: bool = True, api_message: str | None = None) -> InventorySnapshot:
    return InventorySnapshot(
        slots=[],
        systems=[SystemOption(id="system-a", label="System A", platform="core")],
        selected_system_id="system-a",
        selected_system_label="System A",
        selected_system_platform="core",
        layout_slot_count=60,
        refresh_interval_seconds=30,
        warnings=["SES data is partial"],
        sources={
            "api": SourceStatus(enabled=True, ok=api_ok, message=api_message),
            "ssh": SourceStatus(enabled=False, ok=False, message=None),
        },
    )


def _service(**overrides: object) -> Mock:
    service = Mock()
    service.system = SimpleNamespace(id="system-a", truenas=SimpleNamespace(platform="core"))
    service.get_snapshot = AsyncMock(return_value=_snapshot())
    service.get_storage_view_runtime = AsyncMock(
        return_value=StorageViewRuntimePayload(system_id="system-a", views=[])
    )
    for name, value in overrides.items():
        setattr(service, name, value)
    return service


def _registry(service: Mock) -> Mock:
    registry = Mock()
    registry.get_service.return_value = service
    return registry


def _denied(path: str = "/app/data/mappings.json") -> PermissionError:
    return PermissionError(errno.EACCES, "Permission denied", path)


class StartupProbeTests(unittest.TestCase):
    def test_writable_directory_reports_nothing_and_leaves_no_probe_behind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(probe_writable_directories([temp_dir, None, "", temp_dir]), [])
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_missing_directory_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "data"
            self.assertEqual(probe_writable_directories([target]), [])
            self.assertTrue(target.is_dir())

    def test_permission_refusal_names_the_directory_and_the_repair(self) -> None:
        denied = PermissionError(errno.EACCES, "Permission denied")
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("app.services.storage_writability.os.open", side_effect=denied),
        ):
            problems = probe_writable_directories([temp_dir])
        self.assertEqual(len(problems), 1)
        self.assertIn(f"Cannot write to {temp_dir}", problems[0])
        self.assertIn("On the Docker host", problems[0])

    def test_non_permission_errors_are_not_reported_as_unwritable(self) -> None:
        full = OSError(errno.ENOSPC, "No space left on device")
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("app.services.storage_writability.os.open", side_effect=full),
        ):
            self.assertEqual(probe_writable_directories([temp_dir]), [])

    def test_ui_directories_cover_data_logs_and_known_hosts_but_not_config(self) -> None:
        settings = Settings(
            paths=PathConfig(
                mapping_file="/app/data/slot_mappings.json",
                sas_fabric_alias_file="/app/data/sas_fabric_aliases.json",
                slot_detail_cache_file="/app/data/slot_detail_cache.json",
                log_file="/app/logs/app.log",
                profile_file="/app/config/profiles.yaml",
            ),
        )
        directories = app_main.ui_writable_directories(settings)
        self.assertIn("/app/data", directories)
        self.assertIn("/app/logs", directories)
        self.assertNotIn("/app/config", directories)


class ConfiguredKnownHostsStartupTests(unittest.TestCase):
    def settings_with_known_hosts(self, temp_root: Path, known_hosts_path: str) -> Settings:
        data = temp_root / "data"
        data.mkdir(exist_ok=True)
        return Settings(
            config_file=str(temp_root / "config" / "config.yaml"),
            paths=PathConfig(
                mapping_file=str(data / "slot_mappings.json"),
                sas_fabric_alias_file=str(data / "sas_fabric_aliases.json"),
                slot_detail_cache_file=str(data / "slot_detail_cache.json"),
                log_file=str(temp_root / "logs" / "app.log"),
            ),
            ssh=SSHConfig(known_hosts_path=known_hosts_path),
        )

    def test_derived_known_hosts_is_probed_with_the_data_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = self.settings_with_known_hosts(root, str(root / "data" / "known_hosts"))
            derived, configured = app_main.split_known_hosts_paths(settings)
            self.assertEqual(derived, [str(root / "data" / "known_hosts")])
            self.assertEqual(configured, [])
            self.assertIn(str(root / "data"), app_main.ui_writable_directories(settings))

    def test_missing_configured_folder_is_reported_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "host-trust"
            settings = self.settings_with_known_hosts(root, str(missing / "known_hosts"))
            problems = app_route_support.startup_storage_problems(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn("ssh.known_hosts_path", problems[0])
            self.assertIn("does not exist", problems[0])
            self.assertFalse(missing.exists(), "a configured folder must not be created in the container")
            self.assertNotIn(str(missing), app_main.ui_writable_directories(settings))

    def test_configured_writable_folder_reports_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "host-trust").mkdir()
            settings = self.settings_with_known_hosts(root, str(root / "host-trust" / "known_hosts"))
            self.assertEqual(app_route_support.startup_storage_problems(settings), [])

    def test_unwritable_configured_folder_is_reported_with_the_chown_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "host-trust").mkdir()
            settings = self.settings_with_known_hosts(root, str(root / "host-trust" / "known_hosts"))
            with patch("app.services.storage_writability.os.access", return_value=False):
                problems = app_route_support.startup_storage_problems(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn(f"Cannot write to {root / 'host-trust'}", problems[0])

    def test_read_only_configured_file_is_a_warning_not_a_down_problem(self) -> None:
        # Compose mounts /run/ssh read-only; a file pinned there still verifies hosts.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "host-trust").mkdir()
            known_hosts = root / "host-trust" / "known_hosts"
            known_hosts.write_text("", encoding="utf-8")
            settings = self.settings_with_known_hosts(root, str(known_hosts))
            with patch("app.services.storage_writability.os.access", side_effect=lambda p, mode: mode != os.W_OK):
                problems = app_route_support.startup_storage_problems(settings)
                warnings = app_route_support.startup_known_hosts_warnings(settings)
            self.assertEqual(problems, [])
            self.assertEqual(len(warnings), 1)
            self.assertIn("new host keys cannot be saved", warnings[0])

    def test_unreadable_configured_file_is_a_down_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "host-trust").mkdir()
            known_hosts = root / "host-trust" / "known_hosts"
            known_hosts.write_text("", encoding="utf-8")
            settings = self.settings_with_known_hosts(root, str(known_hosts))
            with patch("app.services.storage_writability.os.access", side_effect=lambda p, mode: mode != os.R_OK):
                problems = app_route_support.startup_storage_problems(settings)
                warnings = app_route_support.startup_known_hosts_warnings(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn("not readable by the app", problems[0])
            self.assertEqual(warnings, [])

    def test_untraversable_folder_is_a_problem_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            locked = root / "locked"
            settings = self.settings_with_known_hosts(root, str(locked / "known_hosts"))
            real_is_dir = Path.is_dir

            def is_dir(self: Path) -> bool:
                if self == locked:
                    raise PermissionError(13, "denied")
                return real_is_dir(self)

            with patch("app.services.storage_writability.Path.is_dir", is_dir):
                problems = app_route_support.startup_storage_problems(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn(str(locked), problems[0])

    def test_unwritable_folder_without_a_file_is_still_a_down_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "host-trust").mkdir()
            settings = self.settings_with_known_hosts(root, str(root / "host-trust" / "known_hosts"))
            with patch("app.services.storage_writability.os.access", return_value=False):
                self.assertEqual(len(app_route_support.startup_storage_problems(settings)), 1)
                self.assertEqual(app_route_support.startup_known_hosts_warnings(settings), [])

    def test_per_system_configured_path_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = self.settings_with_known_hosts(root, str(root / "data" / "known_hosts"))
            settings.systems = [
                SystemConfig(id="secondary", ssh=SSHConfig(known_hosts_path=str(root / "absent" / "known_hosts")))
            ]
            problems = app_route_support.startup_storage_problems(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn(str(root / "absent"), problems[0])


class HealthzTests(unittest.TestCase):
    def call_healthz(
        self,
        snapshot: InventorySnapshot | None,
        problems: tuple[str, ...] = (),
        known_hosts_warnings: tuple[str, ...] = (),
    ) -> tuple[int, dict]:
        service = Mock()
        service.peek_cached_snapshot.return_value = snapshot
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    startup_problems=problems,
                    known_hosts_warnings=known_hosts_warnings,
                )
            )
        )
        route = _route("/healthz")
        with patch.object(app_routes, "get_inventory_registry", return_value=_registry(service)):
            response = asyncio.run(route.endpoint(request))
        return response.status_code, json.loads(response.body)

    def test_empty_cache_is_waiting_not_a_problem(self) -> None:
        status, body = self.call_healthz(None)
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "status": "ok",
                "summary": "Waiting for the first inventory",
                "problems": [],
                "dependency_status": "unknown",
                "last_updated": None,
                "sources": {},
                "warnings": [],
                "cache_state": "empty",
            },
        )

    def test_healthy_snapshot_says_all_sources_ok_with_per_source_dump(self) -> None:
        status, body = self.call_healthz(_snapshot())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["summary"], "All sources OK")
        self.assertEqual(body["problems"], [])
        self.assertEqual(body["dependency_status"], "ok")
        self.assertEqual(
            body["sources"],
            {
                "api": {"enabled": True, "ok": True, "message": None},
                "ssh": {"enabled": False, "ok": False, "message": None},
            },
        )
        self.assertEqual(body["warnings"], ["SES data is partial"])
        self.assertEqual(body["cache_state"], "cached")

    def test_unreachable_api_stays_200_but_names_the_reason(self) -> None:
        status, body = self.call_healthz(_snapshot(api_ok=False, api_message="connection refused"))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["dependency_status"], "degraded")
        self.assertEqual(body["summary"], "TrueNAS API degraded: connection refused")
        self.assertEqual(body["problems"], ["TrueNAS API degraded: connection refused"])

    def test_reachable_api_with_degraded_enclosure_data_is_not_called_unreachable(self) -> None:
        message = "TrueNAS API reachable with degraded enclosure data."
        status, body = self.call_healthz(_snapshot(api_ok=False, api_message=message))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["problems"], [f"TrueNAS API degraded: {message}"])
        self.assertNotIn("unreachable", body["summary"])

    def test_unwritable_data_folder_is_down_even_before_the_first_inventory(self) -> None:
        status, body = self.call_healthz(None, problems=(CHOWN_SENTENCE,))
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "down")
        self.assertEqual(body["dependency_status"], "unknown")
        self.assertEqual(body["summary"], f"Data folder not writable: {CHOWN_SENTENCE}")
        self.assertEqual(body["problems"], [CHOWN_SENTENCE])

    def test_unwritable_folder_and_unreachable_api_are_both_listed(self) -> None:
        status, body = self.call_healthz(_snapshot(api_ok=False, api_message=None), problems=(CHOWN_SENTENCE,))
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "down")
        self.assertEqual(body["summary"], f"Data folder not writable: {CHOWN_SENTENCE}")
        self.assertEqual(
            body["problems"],
            [CHOWN_SENTENCE, "TrueNAS API degraded: no details recorded"],
        )

    def test_ssh_and_bmc_failures_are_degraded_not_down(self) -> None:
        snapshot = _snapshot()
        snapshot.sources["ssh"] = SourceStatus(enabled=True, ok=False, message="Authentication failed.")
        snapshot.sources["bmc"] = SourceStatus(enabled=True, ok=False, message=None)
        status, body = self.call_healthz(snapshot)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["dependency_status"], "ok")
        self.assertEqual(
            body["problems"],
            ["SSH degraded: Authentication failed.", "BMC/IPMI degraded: no details recorded"],
        )
        self.assertEqual(body["summary"], "SSH degraded: Authentication failed. (and 1 more)")

    def test_disabled_ssh_is_not_a_problem(self) -> None:
        # _snapshot() carries ssh enabled=False ok=False: turned off, not failing.
        status, body = self.call_healthz(_snapshot())
        self.assertEqual((status, body["status"], body["problems"]), (200, "ok", []))

    def test_unavailable_history_service_is_degraded_not_down(self) -> None:
        problem = "History service unavailable: connection refused."
        with patch.object(app_routes, "history_service_problem", return_value=problem):
            status, body = self.call_healthz(_snapshot())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["summary"], problem)
        self.assertEqual(body["problems"], [problem])

    def test_read_only_pinned_known_hosts_is_degraded_not_down(self) -> None:
        warning = "The known-hosts file /run/ssh/known_hosts is not writable by the app, so new host keys cannot be saved."
        status, body = self.call_healthz(_snapshot(), known_hosts_warnings=(warning,))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["problems"], [warning])

    def test_every_remote_failure_together_is_still_200(self) -> None:
        snapshot = _snapshot(api_ok=False, api_message="connection refused")
        snapshot.sources["ssh"] = SourceStatus(enabled=True, ok=False, message="timed out")
        with patch.object(app_routes, "history_service_problem", return_value="History service unavailable: x"):
            status, body = self.call_healthz(snapshot)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(len(body["problems"]), 3)

    def test_livez_is_unchanged(self) -> None:
        response = asyncio.run(_route("/livez").endpoint())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["status"], "ok")


class StorageReprobeTests(unittest.TestCase):
    def _request(self, directories: tuple[str, ...], problems: tuple[str, ...], checked_at: float) -> SimpleNamespace:
        state = SimpleNamespace(
            startup_problems=problems,
            writable_directories=directories,
            storage_checked_at_monotonic=checked_at,
        )
        return SimpleNamespace(app=SimpleNamespace(state=state))

    def test_recent_probe_is_reused(self) -> None:
        request = self._request(("/app/data",), (CHOWN_SENTENCE,), checked_at=1000.0)
        with (
            patch.object(app_main.time, "monotonic", return_value=1010.0),
            patch.object(app_route_support, "probe_writable_directories") as probe,
        ):
            self.assertEqual(app_route_support.refresh_storage_problems(request), [CHOWN_SENTENCE])
        probe.assert_not_called()

    def test_a_fixed_folder_clears_the_down_state_without_a_restart(self) -> None:
        request = self._request(("/app/data",), (CHOWN_SENTENCE,), checked_at=1000.0)
        with (
            patch.object(app_main.time, "monotonic", return_value=1031.0),
            patch.object(app_route_support, "probe_writable_directories", return_value=[]) as probe,
        ):
            self.assertEqual(app_route_support.refresh_storage_problems(request), [])
        probe.assert_called_once_with(("/app/data",))
        self.assertEqual(request.app.state.startup_problems, ())
        self.assertEqual(request.app.state.storage_checked_at_monotonic, 1031.0)

    def test_a_folder_that_turns_read_only_is_noticed_and_logged_once(self) -> None:
        request = self._request(("/app/data",), (), checked_at=1000.0)
        with (
            patch.object(app_main.time, "monotonic", return_value=1031.0),
            patch.object(app_route_support, "probe_writable_directories", return_value=[CHOWN_SENTENCE]),
            self.assertLogs(app_main.logger, level="ERROR") as logs,
        ):
            self.assertEqual(app_route_support.refresh_storage_problems(request), [CHOWN_SENTENCE])
        self.assertEqual(len(logs.records), 1)

    def test_real_probe_round_trip_on_a_writable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = self._request((temp_dir,), (CHOWN_SENTENCE,), checked_at=0.0)
            self.assertEqual(app_route_support.refresh_storage_problems(request), [])
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_create_app_records_the_probe_directories(self) -> None:
        application = app_main.create_app()
        self.assertTrue(application.state.writable_directories)
        self.assertIsInstance(application.state.storage_checked_at_monotonic, float)
        self.assertIsInstance(application.state.known_hosts_warnings, tuple)

    def test_reprobe_sets_logs_once_and_clears_known_hosts_warnings(self) -> None:
        warning = "The known-hosts file /run/ssh/known_hosts is not writable by the app."
        request = self._request(("/app/data",), (), checked_at=0.0)
        request.app.state.known_hosts_files = ("/run/ssh/known_hosts",)
        request.app.state.known_hosts_warnings = ()
        with (
            patch.object(app_route_support, "probe_writable_directories", return_value=[]),
            patch.object(app_route_support, "check_known_hosts_files", return_value=([], [warning])),
            self.assertLogs(app_main.logger, level="WARNING") as logs,
        ):
            self.assertEqual(app_route_support.refresh_storage_problems(request), [])
            request.app.state.storage_checked_at_monotonic = 0.0
            self.assertEqual(app_route_support.refresh_storage_problems(request), [])
        self.assertEqual([r.getMessage() for r in logs.records], [warning])
        self.assertEqual(request.app.state.known_hosts_warnings, (warning,))
        request.app.state.storage_checked_at_monotonic = 0.0
        with (
            patch.object(app_route_support, "probe_writable_directories", return_value=[]),
            patch.object(app_route_support, "check_known_hosts_files", return_value=([], [])),
            self.assertLogs(app_main.logger, level="INFO") as logs,
        ):
            app_route_support.refresh_storage_problems(request)
        self.assertEqual(request.app.state.known_hosts_warnings, ())
        self.assertIn("Pinned known-hosts files are writable again.", [r.getMessage() for r in logs.records])

    def test_folder_fixed_but_file_read_only_does_not_claim_all_writable(self) -> None:
        warning = "The known-hosts file /run/ssh/known_hosts is not writable by the app."
        request = self._request(("/app/data",), (CHOWN_SENTENCE,), checked_at=0.0)
        request.app.state.known_hosts_files = ("/run/ssh/known_hosts",)
        request.app.state.known_hosts_warnings = ()
        with (
            patch.object(app_route_support, "probe_writable_directories", return_value=[]),
            patch.object(app_route_support, "check_known_hosts_files", return_value=([], [warning])),
            self.assertLogs(app_main.logger, level="INFO") as logs,
        ):
            app_route_support.refresh_storage_problems(request)
        self.assertNotIn(
            "Data, log and known-hosts folders are writable again.",
            [r.getMessage() for r in logs.records],
        )

    def test_config_reload_recomputes_known_hosts_warnings_for_the_new_paths(self) -> None:
        state = SimpleNamespace(known_hosts_warnings=("stale warning",))
        application = SimpleNamespace(state=state)
        settings = Settings()
        with patch.object(app_route_support, "startup_known_hosts_warnings", return_value=[]) as recompute:
            app_route_support.after_config_reload(application, settings, settings)
        recompute.assert_called_once_with(settings)
        self.assertEqual(state.known_hosts_warnings, ())


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class HistoryProbeTests(unittest.TestCase):
    URL = "http://history.example.test:8001"

    def setUp(self) -> None:
        app_route_support.HISTORY_PROBE_CACHE.clear()
        self.addCleanup(app_route_support.HISTORY_PROBE_CACHE.clear)

    def _probe(self, **urlopen: object) -> str | None:
        with patch.object(app_route_support.urllib.request, "urlopen", **urlopen):
            return app_route_support._probe_history_service(self.URL, 2.0)

    def test_healthy_history_is_not_a_problem(self) -> None:
        self.assertIsNone(self._probe(return_value=_FakeResponse(b'{"status": "ok"}')))

    def test_degraded_history_carries_its_detail(self) -> None:
        body = b'{"status": "degraded", "detail": "The last background collection failed."}'
        self.assertEqual(
            self._probe(return_value=_FakeResponse(body)),
            "History service degraded: The last background collection failed.",
        )

    def test_history_storage_fault_is_named(self) -> None:
        error = urllib.error.HTTPError(f"{self.URL}/healthz", 503, "down", {}, io.BytesIO(b"{}"))
        self.assertIn("local storage fault", self._probe(side_effect=error) or "")

    def test_refused_connection_is_named(self) -> None:
        error = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.assertEqual(self._probe(side_effect=error), "History service unavailable: connection refused.")

    def test_timeout_is_named(self) -> None:
        self.assertEqual(
            self._probe(side_effect=TimeoutError()),
            "History service unavailable: no answer within 2 seconds.",
        )

    def test_unresolvable_default_compose_host_means_not_deployed(self) -> None:
        error = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        with patch.object(app_route_support.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(app_route_support._probe_history_service("http://enclosure-history:8001", 2.0))

    def test_unresolvable_custom_host_is_a_problem(self) -> None:
        error = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        self.assertEqual(
            self._probe(side_effect=error),
            "History service unavailable: its host name does not resolve.",
        )

    def test_unreadable_answer_is_named(self) -> None:
        self.assertIn("unreadable", self._probe(return_value=_FakeResponse(b"not json")) or "")

    def test_unconfigured_history_is_not_probed(self) -> None:
        with patch.object(app_route_support, "_probe_history_service") as probe:
            self.assertIsNone(app_route_support.history_service_problem(Settings()))
        probe.assert_not_called()

    def test_answers_are_cached_and_the_timeout_is_capped(self) -> None:
        settings = Settings()
        settings.history.service_url = self.URL
        settings.history.timeout_seconds = 10
        clock = [1000.0]
        with (
            patch.object(app_route_support, "_probe_history_service", return_value="History service unavailable: x") as probe,
            patch.object(app_main.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            app_route_support.history_service_problem(settings)
            clock[0] += 9.0
            app_route_support.history_service_problem(settings)
            self.assertEqual(probe.call_count, 1)
            clock[0] += 2.0
            app_route_support.history_service_problem(settings)
            self.assertEqual(probe.call_count, 2)
        probe.assert_called_with(self.URL, app_route_support.HISTORY_PROBE_MAX_TIMEOUT_SECONDS)


class AdminProbeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        app_route_support.ADMIN_PROBE_CACHE.clear()
        self.addCleanup(app_route_support.ADMIN_PROBE_CACHE.clear)
        # Run the background refresh inline so each TTL test can count probes.
        inline = patch.object(app_route_support, "_start_admin_probe_refresh", side_effect=app_route_support._refresh_admin_probe)
        inline.start()
        self.addCleanup(inline.stop)

    def test_failed_probe_is_remembered_for_ten_seconds(self) -> None:
        clock = [1000.0]
        with (
            patch.object(app_route_support, "_probe_admin_service", return_value=False) as probe,
            patch.object(app_main.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            self.assertFalse(app_route_support.admin_service_reachable("http://admin.example.test:8002", 0.75))
            clock[0] += 9.0
            self.assertFalse(app_route_support.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 1)
            clock[0] += 2.0
            self.assertFalse(app_route_support.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 2)

    def test_successful_probe_is_remembered_for_thirty_seconds(self) -> None:
        clock = [1000.0]
        with (
            patch.object(app_route_support, "_probe_admin_service", return_value=True) as probe,
            patch.object(app_main.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            self.assertTrue(app_route_support.admin_service_reachable("http://admin.example.test:8002", 0.75))
            clock[0] += 29.0
            self.assertTrue(app_route_support.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 1)
            clock[0] += 2.0
            self.assertTrue(app_route_support.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 2)

    def test_cache_is_per_service_url(self) -> None:
        with patch.object(app_route_support, "_probe_admin_service", side_effect=[False, True]) as probe:
            self.assertFalse(app_route_support.admin_service_reachable("http://admin-a.example.test:8002", 0.75))
            self.assertTrue(app_route_support.admin_service_reachable("http://admin-b.example.test:8002", 0.75))
        self.assertEqual(probe.call_count, 2)

    def test_launch_state_reports_stopped_when_configured_but_unreachable(self) -> None:
        settings = Settings()
        settings.admin.service_url = "http://enclosure-admin:8002"
        with patch.object(app_route_support, "_probe_admin_service", return_value=False):
            state = app_route_support.resolve_admin_launch_url(_request(), settings)
        self.assertEqual(state, app_route_support.AdminLaunchState(url=None, stopped=True))

    def test_launch_state_carries_the_url_when_admin_answers(self) -> None:
        settings = Settings()
        settings.admin.service_url = "http://enclosure-admin:8002"
        settings.admin.port = 8082
        with patch.object(app_route_support, "_probe_admin_service", return_value=True):
            state = app_route_support.resolve_admin_launch_url(_request(), settings)
        self.assertEqual(state, app_route_support.AdminLaunchState(url="http://testserver:8082", stopped=False))

    def test_launch_state_is_none_when_admin_is_not_configured(self) -> None:
        with patch.object(app_route_support, "_probe_admin_service") as probe:
            self.assertIsNone(app_route_support.resolve_admin_launch_url(_request(), Settings()))
        probe.assert_not_called()


class AdminProbeHotPathTests(unittest.TestCase):
    """A page load never waits for an admin probe once there is an answer (#453)."""

    URL = "http://admin.example.test:8002"

    def setUp(self) -> None:
        app_route_support.ADMIN_PROBE_CACHE.clear()
        self.addCleanup(app_route_support.ADMIN_PROBE_CACHE.clear)
        app_route_support._ADMIN_PROBE_REFRESHING.clear()
        self.addCleanup(app_route_support._ADMIN_PROBE_REFRESHING.clear)

    def _expire(self) -> None:
        for entry in app_route_support.ADMIN_PROBE_CACHE.values():
            entry.expires_at_monotonic = 0.0

    def test_expired_answer_is_served_at_once_and_refreshed_in_the_background(self) -> None:
        release = threading.Event()
        started = threading.Event()
        answers = iter([False, True])

        def probe(_url: str, _timeout: float) -> bool:
            answer = next(answers)
            if answer:
                started.set()
                release.wait(5)
            return answer

        with patch.object(app_route_support, "_probe_admin_service", side_effect=probe) as mocked:
            self.assertFalse(app_route_support.admin_service_reachable(self.URL, 0.75))
            self._expire()
            begun = time.perf_counter()
            # The slow probe is still blocked, yet the lookup answers with the last state.
            self.assertFalse(app_route_support.admin_service_reachable(self.URL, 0.75))
            self.assertLess(time.perf_counter() - begun, 0.5)
            self.assertTrue(started.wait(5))
            # A second expired lookup while that probe runs starts no other probe.
            self.assertFalse(app_route_support.admin_service_reachable(self.URL, 0.75))
            release.set()
            for _ in range(200):
                if self.URL not in app_route_support._ADMIN_PROBE_REFRESHING:
                    break
                time.sleep(0.01)
            self.assertEqual(mocked.call_count, 2)
        self.assertTrue(app_route_support.admin_service_reachable(self.URL, 0.75))

    def test_failed_background_refresh_keeps_serving_and_retries(self) -> None:
        with patch.object(app_route_support, "_probe_admin_service", return_value=True):
            self.assertTrue(app_route_support.admin_service_reachable(self.URL, 0.75))
        self._expire()
        with (
            patch.object(app_route_support, "_probe_admin_service", side_effect=RuntimeError("synthetic")),
            patch.object(app_route_support, "_start_admin_probe_refresh",
                         side_effect=lambda url, timeout: self.assertRaises(
                             RuntimeError, app_route_support._refresh_admin_probe, url, timeout)),
        ):
            self.assertTrue(app_route_support.admin_service_reachable(self.URL, 0.75))
        # The refresh marker is cleared, so the next lookup can try again.
        self.assertNotIn(self.URL, app_route_support._ADMIN_PROBE_REFRESHING)

    def test_refresh_landing_before_the_lock_is_not_repeated(self) -> None:
        """A caller that read the expired entry re-checks the cache under the lock."""

        with patch.object(app_route_support, "_probe_admin_service", return_value=False):
            self.assertFalse(app_route_support.admin_service_reachable(self.URL, 0.75))
        self._expire()
        url = self.URL
        real_lock = app_route_support._ADMIN_PROBE_LOCK

        class RefreshLandsFirst:
            """Lets another caller's refresh finish between the unlocked read and the lock."""

            landed = False

            def __enter__(self):
                if not RefreshLandsFirst.landed:
                    RefreshLandsFirst.landed = True
                    app_route_support.ADMIN_PROBE_CACHE[url] = app_route_support.AdminProbeCacheEntry(
                        reachable=True, expires_at_monotonic=time.monotonic() + 30,
                    )
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        with (
            patch.object(app_route_support, "_ADMIN_PROBE_LOCK", RefreshLandsFirst()),
            patch.object(app_route_support, "_probe_admin_service") as probe,
            patch.object(app_route_support, "_start_admin_probe_refresh") as start,
        ):
            # The fresh answer wins over the stale pre-lock read; no second probe.
            self.assertTrue(app_route_support.admin_service_reachable(self.URL, 0.75))
        probe.assert_not_called()
        start.assert_not_called()
        self.assertNotIn(self.URL, app_route_support._ADMIN_PROBE_REFRESHING)

    def test_concurrent_expired_lookups_start_one_refresh(self) -> None:
        with patch.object(app_route_support, "_probe_admin_service", return_value=False):
            self.assertFalse(app_route_support.admin_service_reachable(self.URL, 0.75))
        self._expire()
        gate = threading.Barrier(16)
        results: list[bool] = []
        results_lock = threading.Lock()

        def page_load() -> None:
            gate.wait(5)
            answer = app_route_support.admin_service_reachable(self.URL, 0.75)
            with results_lock:
                results.append(answer)

        with patch.object(app_route_support, "_probe_admin_service", return_value=True) as probe:
            for _round in range(20):
                threads = [threading.Thread(target=page_load) for _ in range(16)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                gate.reset()
                for _ in range(200):
                    if self.URL not in app_route_support._ADMIN_PROBE_REFRESHING:
                        break
                    time.sleep(0.005)
                # Every round after the first sees a fresh answer: no more probes.
                self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(results), 16 * 20)
        self.assertTrue(app_route_support.admin_service_reachable(self.URL, 0.75))

    def test_startup_warm_fills_the_cache_before_the_first_page(self) -> None:
        settings = Settings()
        settings.admin.service_url = self.URL
        with patch.object(app_route_support, "_probe_admin_service", return_value=False) as probe:
            app_main.warm_admin_probe(settings)
            self.assertIn(self.URL, app_route_support.ADMIN_PROBE_CACHE)
            app_route_support.resolve_admin_launch_url(_request(), settings)
        self.assertEqual(probe.call_count, 1)

    def test_startup_warm_skips_an_unconfigured_admin(self) -> None:
        with patch.object(app_route_support, "_probe_admin_service") as probe:
            app_main.warm_admin_probe(Settings())
        probe.assert_not_called()


class IndexPageTests(unittest.TestCase):
    def render_index(self, admin_state, *, startup_problems: tuple[str, ...] = ()) -> str:
        settings = Settings(
            systems=[SystemConfig(id="system-a", label="System A")],
            default_system_id="system-a",
        )
        release_service = Mock()
        release_service.snapshot.return_value = {}
        route = _route("/")
        previous_problems = getattr(app_main.app.state, "startup_problems", ())
        app_main.app.state.startup_problems = startup_problems
        try:
            with (
                patch.object(app_routes, "get_settings", return_value=settings),
                patch.object(app_routes, "get_inventory_registry", return_value=_registry(_service())),
                patch.object(app_routes, "get_release_status_service", return_value=release_service),
                patch.object(app_routes, "resolve_admin_launch_url", return_value=admin_state),
            ):
                response = asyncio.run(route.endpoint(request=_request(), system_id=None, enclosure_id=None))
        finally:
            app_main.app.state.startup_problems = previous_problems
        self.assertEqual(response.status_code, 200)
        return response.body.decode("utf-8")

    def test_stopped_admin_renders_a_disabled_button_with_the_restart_command(self) -> None:
        page = self.render_index(app_route_support.AdminLaunchState(url=None, stopped=True))
        self.assertIn('id="admin-launch-stopped"', page)
        self.assertIn(">System Setup</button>", page)
        self.assertIn("disabled", page.split('id="admin-launch-stopped"', 1)[1].split("</button>", 1)[0])
        self.assertIn("Admin is not running. It may have stopped on its own: the published Compose files stop it after an hour by default.", page)
        self.assertIn("docker compose --profile admin up -d enclosure-admin", page)
        self.assertNotIn('href="http://testserver:8082"', page)

    def test_running_admin_keeps_the_link(self) -> None:
        page = self.render_index(app_route_support.AdminLaunchState(url="http://testserver:8082", stopped=False))
        self.assertIn('href="http://testserver:8082"', page)
        self.assertNotIn("Admin is not running", page)

    def test_unconfigured_admin_hides_the_button(self) -> None:
        page = self.render_index(None)
        self.assertNotIn("System Setup", page)

    def test_startup_problems_appear_in_the_warnings_panel_and_bootstrap(self) -> None:
        page = self.render_index(None, startup_problems=(CHOWN_SENTENCE,))
        self.assertIn(f'<div class="warning-item">{CHOWN_SENTENCE}</div>', page)
        self.assertIn("SES data is partial", page)
        self.assertLess(page.index(CHOWN_SENTENCE), page.index("SES data is partial"))


if __name__ == "__main__":
    unittest.main()
