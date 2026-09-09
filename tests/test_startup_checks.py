from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
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
from app.config import PathConfig, Settings, SystemConfig
from app.models.domain import (
    InventorySnapshot,
    MappingBundle,
    MappingImportConfirmation,
    MappingRequest,
    SasFabricAliasRequest,
    SourceStatus,
    StorageViewRuntimePayload,
    SystemOption,
)
from app.startup_checks import (
    check_writable_dirs,
    describe_unwritable_dir,
    history_writable_directories,
    is_unwritable_path_error,
    ui_writable_directories,
)
from history_service.config import HistorySettings

REVISION = "a" * 64
CHOWN_SENTENCE = (
    "Cannot write to /app/data (owned by uid 0, running as uid 10001). "
    "On the Docker host run: sudo chown -R 10001:10001 data"
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


class WritabilityProbeTests(unittest.TestCase):
    def test_writable_folder_reports_nothing_and_leaves_no_probe_behind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(check_writable_dirs([temp_dir]), [])
            self.assertEqual(os.listdir(temp_dir), [])

    def test_file_where_a_folder_should_be_reports_one_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blocked = Path(temp_dir) / "data"
            blocked.write_text("not a folder", encoding="utf-8")

            problems = check_writable_dirs([blocked, str(blocked)])

        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith(f"Cannot write to {blocked}"), problems[0])
        self.assertIn("data", problems[0])

    def test_missing_and_blank_entries_are_skipped_and_created_when_possible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fresh = Path(temp_dir) / "logs"
            self.assertEqual(check_writable_dirs([None, "", fresh]), [])
            self.assertTrue(fresh.is_dir())

    @unittest.skipIf(sys.platform == "win32", "directory modes are a POSIX concept")
    @unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0, "root ignores directory modes")
    def test_read_only_folder_names_the_chown_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            locked = Path(temp_dir) / "history"
            locked.mkdir()
            locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                problems = check_writable_dirs([locked])
            finally:
                locked.chmod(stat.S_IRWXU)

        uid, gid = os.geteuid(), os.getegid()
        self.assertEqual(
            problems,
            [
                f"Cannot write to {locked} (owned by uid {uid}, running as uid {uid}). "
                f"On the Docker host run: sudo chown -R {uid}:{gid} history"
            ],
        )

    def test_sentence_shape_matches_the_documented_example(self) -> None:
        sentence = describe_unwritable_dir(
            "/app/data",
            _denied("/app/data/.write-probe"),
            owner_uid=0,
            process_ids=(10001, 10001),
        )
        self.assertEqual(sentence, CHOWN_SENTENCE)

    def test_sentence_without_process_ids_still_names_the_folder(self) -> None:
        sentence = describe_unwritable_dir(
            "/app/logs",
            _denied("/app/logs/.write-probe"),
            owner_uid=None,
            process_ids=None,
        )
        self.assertEqual(
            sentence,
            "Cannot write to /app/logs (Permission denied). Make sure logs exists and is writable by the app.",
        )

    def test_read_only_mount_gets_its_own_sentence(self) -> None:
        sentence = describe_unwritable_dir(
            "/app/config",
            OSError(errno.EROFS, "Read-only file system", "/app/config/.write-probe"),
            owner_uid=0,
            process_ids=(10001, 10001),
        )
        self.assertIn("mounted read-only", sentence)
        self.assertIn("docker-compose.yml", sentence)
        self.assertNotIn("chown", sentence)

    def test_only_permission_shaped_errors_count_as_unwritable(self) -> None:
        self.assertTrue(is_unwritable_path_error(_denied()))
        self.assertTrue(is_unwritable_path_error(OSError(errno.EROFS, "Read-only file system")))
        self.assertFalse(is_unwritable_path_error(ConnectionRefusedError(errno.ECONNREFUSED, "refused")))
        self.assertFalse(is_unwritable_path_error(TimeoutError()))
        self.assertFalse(is_unwritable_path_error(OSError(errno.ENOSPC, "No space left on device")))
        self.assertFalse(is_unwritable_path_error(RuntimeError("not an OSError")))

    def test_ui_folders_are_the_data_and_logs_folders_once_each_and_never_the_config_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = Settings(
                paths=PathConfig(
                    mapping_file=str(root / "data" / "mappings.json"),
                    sas_fabric_alias_file=str(root / "data" / "aliases.json"),
                    profile_file=str(root / "config" / "profiles.yaml"),
                    slot_detail_cache_file=str(root / "data" / "slot-details.json"),
                    log_file=str(root / "logs" / "app.log"),
                )
            )
            settings.ssh.known_hosts_path = str(root / "data" / "known_hosts")
            self.assertEqual(
                ui_writable_directories(settings),
                [str(root / "data"), str(root / "logs")],
            )

    def test_history_folders_are_the_database_and_backup_folders(self) -> None:
        settings = HistorySettings(
            sqlite_path="/app/history/history.db",
            backup_dir="/app/history/backups",
            long_term_backup_dir="/app/history/backups/long-term",
        )
        self.assertEqual(
            history_writable_directories(settings),
            ["/app/history", "/app/history/backups", "/app/history/backups/long-term"],
        )

    def test_history_service_checks_folders_before_building_the_store(self) -> None:
        source = Path("history_service/main.py").read_text(encoding="utf-8")
        check_index = source.index("check_writable_dirs(history_writable_directories(settings))")
        store_index = source.index("store = build_history_store(settings)")
        self.assertLess(check_index, store_index)


class UnwritableSaveRouteTests(unittest.TestCase):
    def assert_unwritable_response(self, response) -> None:
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.body)
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["error"], "data_folder_not_writable")
        self.assertEqual(
            body["detail"],
            "Could not save: the data folder is not writable by the app. See Troubleshooting.",
        )

    def test_mapping_save_answers_503_when_the_data_folder_refuses_the_write(self) -> None:
        service = _service(save_mapping=AsyncMock(side_effect=_denied()))
        route = _route("/api/slots/{slot}/mapping", "POST")

        with (
            patch.object(app_main, "get_inventory_registry", return_value=_registry(service)),
            patch.object(app_main, "ensure_slot_bounds", AsyncMock(return_value=None)),
        ):
            response = asyncio.run(
                route.endpoint(slot=3, payload=MappingRequest(expected_revision=REVISION, serial="SN-0001"))
            )

        self.assert_unwritable_response(response)
        service.get_snapshot.assert_not_awaited()

    def test_mapping_save_keeps_network_errors_as_unhandled(self) -> None:
        service = _service(save_mapping=AsyncMock(side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "refused")))
        route = _route("/api/slots/{slot}/mapping", "POST")

        with (
            patch.object(app_main, "get_inventory_registry", return_value=_registry(service)),
            patch.object(app_main, "ensure_slot_bounds", AsyncMock(return_value=None)),
            self.assertRaises(ConnectionRefusedError),
        ):
            asyncio.run(route.endpoint(slot=3, payload=MappingRequest(expected_revision=REVISION, serial="SN-0001")))

    def test_mapping_clear_answers_503_when_the_data_folder_refuses_the_write(self) -> None:
        service = _service(clear_mapping=AsyncMock(side_effect=_denied()))
        route = _route("/api/slots/{slot}/mapping", "DELETE")

        with (
            patch.object(app_main, "get_inventory_registry", return_value=_registry(service)),
            patch.object(app_main, "ensure_slot_bounds", AsyncMock(return_value=None)),
        ):
            response = asyncio.run(route.endpoint(slot=3, expected_revision=REVISION))

        self.assert_unwritable_response(response)

    def test_alias_save_answers_503_when_the_data_folder_refuses_the_write(self) -> None:
        service = _service(save_sas_fabric_alias=Mock(side_effect=_denied("/app/data/aliases.json")))
        route = _route("/api/sas-fabric/aliases", "POST")

        with patch.object(app_main, "get_inventory_registry", return_value=_registry(service)):
            response = asyncio.run(
                route.endpoint(payload=SasFabricAliasRequest(object_id="expander-0001", label="Shelf A"))
            )

        self.assert_unwritable_response(response)

    def test_mapping_import_answers_503_when_the_data_folder_refuses_the_write(self) -> None:
        service = _service(import_mapping_bundle=AsyncMock(side_effect=_denied()))
        route = _route("/api/mappings/import", "POST")
        confirmation = MappingImportConfirmation(
            bundle=MappingBundle(),
            expected_revision=REVISION,
            import_digest="b" * 64,
            confirmed=True,
        )

        with patch.object(app_main, "get_inventory_registry", return_value=_registry(service)):
            response = asyncio.run(route.endpoint(payload=confirmation))

        self.assert_unwritable_response(response)


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
        self.assertEqual(body["summary"], "TrueNAS API unreachable: connection refused")
        self.assertEqual(body["problems"], ["TrueNAS API unreachable: connection refused"])

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
            [CHOWN_SENTENCE, "TrueNAS API unreachable: no details recorded"],
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
        self.assertIn("Admin is stopped (it stops itself after an hour).", page)
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
