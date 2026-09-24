from __future__ import annotations

import asyncio
import errno
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Request
from fastapi.routing import APIRoute

# Must precede admin_service.main, which builds its app at import time.
import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
from app import main as app_main
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
            problems = app_main.startup_storage_problems(settings)
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
            self.assertEqual(app_main.startup_storage_problems(settings), [])

    def test_unwritable_configured_folder_is_reported_with_the_chown_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "host-trust").mkdir()
            settings = self.settings_with_known_hosts(root, str(root / "host-trust" / "known_hosts"))
            with patch("app.services.storage_writability.os.access", return_value=False):
                problems = app_main.startup_storage_problems(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn(f"Cannot write to {root / 'host-trust'}", problems[0])

    def test_unwritable_configured_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "host-trust").mkdir()
            known_hosts = root / "host-trust" / "known_hosts"
            known_hosts.write_text("", encoding="utf-8")
            settings = self.settings_with_known_hosts(root, str(known_hosts))
            with patch("app.services.storage_writability.os.access", return_value=False):
                problems = app_main.startup_storage_problems(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn("new host keys cannot be saved", problems[0])

    def test_per_system_configured_path_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = self.settings_with_known_hosts(root, str(root / "data" / "known_hosts"))
            settings.systems = [
                SystemConfig(id="secondary", ssh=SSHConfig(known_hosts_path=str(root / "absent" / "known_hosts")))
            ]
            problems = app_main.startup_storage_problems(settings)
            self.assertEqual(len(problems), 1)
            self.assertIn(str(root / "absent"), problems[0])


class HealthzTests(unittest.TestCase):
    def call_healthz(self, snapshot: InventorySnapshot | None, problems: tuple[str, ...] = ()) -> tuple[int, dict]:
        service = Mock()
        service.peek_cached_snapshot.return_value = snapshot
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(startup_problems=problems)))
        route = _route("/healthz")
        with patch.object(app_main, "get_inventory_registry", return_value=_registry(service)):
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

    def test_unwritable_data_folder_is_degraded_even_before_the_first_inventory(self) -> None:
        status, body = self.call_healthz(None, problems=(CHOWN_SENTENCE,))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["dependency_status"], "unknown")
        self.assertEqual(body["summary"], f"Data folder not writable: {CHOWN_SENTENCE}")
        self.assertEqual(body["problems"], [CHOWN_SENTENCE])

    def test_unwritable_folder_and_unreachable_api_are_both_listed(self) -> None:
        status, body = self.call_healthz(_snapshot(api_ok=False, api_message=None), problems=(CHOWN_SENTENCE,))
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"], f"Data folder not writable: {CHOWN_SENTENCE}")
        self.assertEqual(
            body["problems"],
            [CHOWN_SENTENCE, "TrueNAS API degraded: no details recorded"],
        )

    def test_livez_is_unchanged(self) -> None:
        response = asyncio.run(_route("/livez").endpoint())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["status"], "ok")


class AdminProbeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        app_main.ADMIN_PROBE_CACHE.clear()
        self.addCleanup(app_main.ADMIN_PROBE_CACHE.clear)

    def test_failed_probe_is_remembered_for_ten_seconds(self) -> None:
        clock = [1000.0]
        with (
            patch.object(app_main, "_probe_admin_service", return_value=False) as probe,
            patch.object(app_main.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            self.assertFalse(app_main.admin_service_reachable("http://admin.example.test:8002", 0.75))
            clock[0] += 9.0
            self.assertFalse(app_main.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 1)
            clock[0] += 2.0
            self.assertFalse(app_main.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 2)

    def test_successful_probe_is_remembered_for_thirty_seconds(self) -> None:
        clock = [1000.0]
        with (
            patch.object(app_main, "_probe_admin_service", return_value=True) as probe,
            patch.object(app_main.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            self.assertTrue(app_main.admin_service_reachable("http://admin.example.test:8002", 0.75))
            clock[0] += 29.0
            self.assertTrue(app_main.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 1)
            clock[0] += 2.0
            self.assertTrue(app_main.admin_service_reachable("http://admin.example.test:8002", 0.75))
            self.assertEqual(probe.call_count, 2)

    def test_cache_is_per_service_url(self) -> None:
        with patch.object(app_main, "_probe_admin_service", side_effect=[False, True]) as probe:
            self.assertFalse(app_main.admin_service_reachable("http://admin-a.example.test:8002", 0.75))
            self.assertTrue(app_main.admin_service_reachable("http://admin-b.example.test:8002", 0.75))
        self.assertEqual(probe.call_count, 2)

    def test_launch_state_reports_stopped_when_configured_but_unreachable(self) -> None:
        settings = Settings()
        settings.admin.service_url = "http://enclosure-admin:8002"
        with patch.object(app_main, "_probe_admin_service", return_value=False):
            state = app_main.resolve_admin_launch_url(_request(), settings)
        self.assertEqual(state, app_main.AdminLaunchState(url=None, stopped=True))

    def test_launch_state_carries_the_url_when_admin_answers(self) -> None:
        settings = Settings()
        settings.admin.service_url = "http://enclosure-admin:8002"
        settings.admin.port = 8082
        with patch.object(app_main, "_probe_admin_service", return_value=True):
            state = app_main.resolve_admin_launch_url(_request(), settings)
        self.assertEqual(state, app_main.AdminLaunchState(url="http://testserver:8082", stopped=False))

    def test_launch_state_is_none_when_admin_is_not_configured(self) -> None:
        with patch.object(app_main, "_probe_admin_service") as probe:
            self.assertIsNone(app_main.resolve_admin_launch_url(_request(), Settings()))
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
                patch.object(app_main, "get_settings", return_value=settings),
                patch.object(app_main, "get_inventory_registry", return_value=_registry(_service())),
                patch.object(app_main, "get_release_status_service", return_value=release_service),
                patch.object(app_main, "resolve_admin_launch_url", return_value=admin_state),
            ):
                response = asyncio.run(route.endpoint(request=_request(), system_id=None, enclosure_id=None))
        finally:
            app_main.app.state.startup_problems = previous_problems
        self.assertEqual(response.status_code, 200)
        return response.body.decode("utf-8")

    def test_stopped_admin_renders_a_disabled_button_with_the_restart_command(self) -> None:
        page = self.render_index(app_main.AdminLaunchState(url=None, stopped=True))
        self.assertIn('id="admin-launch-stopped"', page)
        self.assertIn(">System Setup</button>", page)
        self.assertIn("disabled", page.split('id="admin-launch-stopped"', 1)[1].split("</button>", 1)[0])
        self.assertIn("Admin is stopped (it stops itself when idle).", page)
        self.assertIn("docker compose --profile admin up -d enclosure-admin", page)
        self.assertNotIn('href="http://testserver:8082"', page)

    def test_running_admin_keeps_the_link(self) -> None:
        page = self.render_index(app_main.AdminLaunchState(url="http://testserver:8082", stopped=False))
        self.assertIn('href="http://testserver:8082"', page)
        self.assertNotIn("Admin is stopped", page)

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
