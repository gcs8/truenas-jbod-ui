from __future__ import annotations

import asyncio
import json
import shlex
import stat
import tempfile
import threading
import urllib.error
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from app import __version__
from admin_service.config import AdminSettings
from admin_service.services.esxi_host_prep import MAX_UPLOAD_BYTES
from admin_service.services.runtime_control import DockerRuntimeService
from admin_service.main import app as admin_app
from admin_service.main import annotate_runtime_versions
from admin_service.main import build_admin_state_payload
from admin_service.main import decode_optional_secret_header
from admin_service.main import enrich_quantastor_nodes_from_ssh
from admin_service.main import get_history_store
from admin_service.main import stream_limited_request_body_to_file
from admin_service.main import templates as admin_templates
from app.config import (
    AdminSurfaceConfig,
    BMCConfig,
    EnclosureProfileConfig,
    PathConfig,
    SSHConfig,
    Settings,
    SystemConfig,
    TrueNASConfig,
)
from app.main import app as main_app
from app.main import resolve_admin_launch_url
from app.main import _clear_snapshot_export_source_cache_for_tests
from app.models.domain import ESXiHostPrepInstallRequest
from app.models.domain import EnclosureOption
from app.models.domain import EnclosureProfileRequest
from app.models.domain import HistoryAdoptRequest
from app.models.domain import QuantastorNodeDiscoveryRequest
from app.models.domain import SnapshotExportRequest
from app.models.domain import SystemSetupBootstrapRequest
from app.models.domain import SystemSetupRequest
from app.models.domain import SystemSetupSudoPreviewRequest
from app.models.domain import SystemBackupExportRequest
from app.services.profile_registry import UNIFI_UNVR_FRONT_4_PROFILE_ID
from app.services.ssh_probe import SSHCommandResult
from app.services.snapshot_export import PackagedSnapshotExport
from app.services.system_setup import PRESERVE_SECRET_SENTINEL
from history_service.config import HistorySettings
from history_service.main import app as history_app
from history_service.system_backup import FileBackupArtifact
from app.services.truenas_ws import TrueNASRawData


MARKER_ALPHA = "marker-alpha"
MARKER_BRAVO = "marker-bravo"
MARKER_CHARLIE = "marker-charlie"
MARKER_DELTA = "marker-delta"
MARKER_ECHO = "marker-echo"


def make_request(host: str = "localhost", port: int = 8082) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"host", f"{host}:{port}".encode("ascii"))],
            "client": ("127.0.0.1", 12345),
            "server": (host, port),
        }
    )


def make_streaming_request(
    chunks: list[bytes],
    *,
    content_length: int | None = None,
) -> tuple[Request, MagicMock]:
    receive_probe = MagicMock()
    pending = list(chunks)

    async def receive() -> dict[str, object]:
        receive_probe()
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.request", "body": b"", "more_body": False}

    headers = [(b"host", b"admin.example.test")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/admin/backup/import",
            "raw_path": b"/api/admin/backup/import",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("admin.example.test", 80),
        },
        receive,
    )
    return request, receive_probe


class BackupImportRequestLimitTests(unittest.TestCase):
    def test_declared_oversize_body_is_rejected_without_reading_stream(self) -> None:
        request, receive_probe = make_streaming_request([b"ignored"], content_length=5)

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(stream_limited_request_body_to_file(request, max_bytes=4))

        self.assertEqual(raised.exception.status_code, 413)
        receive_probe.assert_not_called()

    def test_chunked_body_stops_when_cumulative_limit_is_crossed(self) -> None:
        request, receive_probe = make_streaming_request([b"abc", b"def"])

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(stream_limited_request_body_to_file(request, max_bytes=4))

        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(receive_probe.call_count, 2)

    def test_file_backed_body_streams_to_private_temporary_file(self) -> None:
        request, receive_probe = make_streaming_request([b"abc", b"def"])

        archive_path = asyncio.run(
            stream_limited_request_body_to_file(request, max_bytes=6)
        )
        try:
            self.assertEqual(archive_path.read_bytes(), b"abcdef")
            self.assertEqual(stat.S_IMODE(archive_path.stat().st_mode), 0o600)
            self.assertEqual(receive_probe.call_count, 2)
        finally:
            archive_path.unlink(missing_ok=True)
            archive_path.parent.rmdir()

    def test_cancelled_body_stream_cleans_private_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "cancelled-request"
            workspace.mkdir()
            receive_count = 0

            async def receive() -> dict[str, object]:
                nonlocal receive_count
                receive_count += 1
                if receive_count == 1:
                    return {"type": "http.request", "body": b"abc", "more_body": True}
                raise asyncio.CancelledError()

            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/api/admin/backup/import",
                    "raw_path": b"/api/admin/backup/import",
                    "query_string": b"",
                    "headers": [(b"host", b"admin.example.test")],
                    "client": ("127.0.0.1", 12345),
                    "server": ("admin.example.test", 80),
                },
                receive,
            )

            with patch("admin_service.main.tempfile.mkdtemp", return_value=str(workspace)):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(stream_limited_request_body_to_file(request, max_bytes=4))

            self.assertFalse(workspace.exists())


class MainAppBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        _clear_snapshot_export_source_cache_for_tests()

    @staticmethod
    def _call_main_route(path: str) -> object:
        route = next(route for route in main_app.routes if route.path == path)
        return asyncio.run(route.endpoint())

    def test_admin_sidecar_exposes_one_time_bootstrap_route(self) -> None:
        paths = {route.path for route in admin_app.routes}

        self.assertIn("/api/admin/system-setup/bootstrap", paths)
        self.assertIn("/api/admin/system-setup/sudoers-preview", paths)
        self.assertIn("/api/admin/esxi-host-prep/upload", paths)
        self.assertIn("/api/admin/esxi-host-prep/install", paths)
        self.assertIn("/api/admin/system-setup/{system_id}", paths)
        self.assertIn("/api/admin/system-setup/demo", paths)
        self.assertIn("/api/admin/profiles", paths)
        self.assertIn("/api/admin/profiles/{profile_id}", paths)
        self.assertIn("/api/admin/history/purge-orphaned", paths)
        self.assertIn("/api/admin/history/orphaned", paths)
        self.assertIn("/api/admin/history/adopt-removed-system", paths)
        self.assertIn("/api/admin/debug/export", paths)
        self.assertIn("/api/admin/runtime-behavior", paths)

    def test_admin_history_store_disables_destructive_database_recovery(self) -> None:
        get_history_store.cache_clear()
        try:
            with (
                patch(
                    "admin_service.main.get_history_settings",
                    return_value=SimpleNamespace(
                        sqlite_path="/tmp/admin-history.sqlite3",
                        segment_catalog_path="/tmp/admin-history-segments/catalog.json",
                    ),
                ),
                patch("admin_service.main.HistoryStore") as history_store,
            ):
                get_history_store()

            history_store.assert_called_once_with(
                "/tmp/admin-history.sqlite3",
                recover_unreadable_database=False,
                segment_catalog_path="/tmp/admin-history-segments/catalog.json",
            )
        finally:
            get_history_store.cache_clear()

    def test_main_app_does_not_expose_embedded_admin_routes(self) -> None:
        paths = {route.path for route in main_app.routes}

        self.assertNotIn("/api/system-backup/export", paths)
        self.assertNotIn("/api/system-backup/import", paths)
        self.assertNotIn("/api/system-setup", paths)
        self.assertNotIn("/api/system-setup/ssh-keys", paths)
        self.assertNotIn("/api/system-setup/ssh-keys/generate", paths)

    def test_history_service_does_not_expose_backup_mutation_routes(self) -> None:
        paths = {route.path for route in history_app.routes}

        self.assertNotIn("/api/system/backup/export", paths)
        self.assertNotIn("/api/system/backup/import", paths)

    def test_admin_backup_export_returns_file_response_and_cleans_workspace(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="admin-export-response-"))
        archive_path = workspace / "backup.zip"
        archive_path.write_bytes(b"PK-file-backed")
        artifact = FileBackupArtifact(
            filename="backup.zip",
            path=archive_path,
            media_type="application/zip",
            manifest={"packaging": "zip", "schema_version": 1},
            cleanup_root=workspace,
        )
        maintenance = SimpleNamespace(
            stopped_containers=[],
            restarted_containers=[],
            restart_failures={},
        )
        service = MagicMock()
        service.export_bundle.return_value = (artifact, maintenance)
        route = next(route for route in admin_app.routes if route.path == "/api/admin/backup/export")

        with patch("admin_service.main.get_maintenance_service", return_value=service):
            response = asyncio.run(
                route.endpoint(
                    SystemBackupExportRequest(
                        packaging="zip",
                        encrypt=True,
                        passphrase="test-passphrase",
                    ),
                    stop_services=False,
                    restart_services=True,
                )
            )

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="backup.zip"')
        self.assertTrue(archive_path.exists())

        async def disconnect_during_body() -> None:
            async def receive() -> dict[str, object]:
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                if message.get("type") == "http.response.body":
                    raise asyncio.CancelledError()

            await response(
                {"type": "http", "method": "GET", "headers": []},
                receive,
                send,
            )

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(disconnect_during_body())
        self.assertFalse(workspace.exists())

    def test_admin_backup_import_streams_file_and_cleans_workspace(self) -> None:
        request, _receive_probe = make_streaming_request([b"archive-", b"bytes"])
        maintenance = SimpleNamespace(
            stopped_containers=[],
            restarted_containers=[],
            restart_failures={},
        )
        service = MagicMock()
        observed: dict[str, object] = {}

        def import_from_file(path: Path, **kwargs: object) -> tuple[dict[str, object], object]:
            observed["path"] = path
            observed["content"] = path.read_bytes()
            observed["mode"] = stat.S_IMODE(path.stat().st_mode)
            return (
                {
                    "ok": True,
                    "systems": [],
                    "restored_paths": [],
                    "preserved_absent_groups": [],
                },
                maintenance,
            )

        service.import_bundle_from_file.side_effect = import_from_file
        service.import_bundle.side_effect = AssertionError("byte import must not be used")
        route = next(route for route in admin_app.routes if route.path == "/api/admin/backup/import")
        runtime_service = MagicMock()
        runtime_service.managed_containers = {}

        with (
            patch("admin_service.main.get_maintenance_service", return_value=service),
            patch(
                "admin_service.main.reload_app_settings",
                return_value=SimpleNamespace(default_system_id=None),
            ),
            patch(
                "admin_service.main.get_runtime_service",
                return_value=runtime_service,
            ),
            patch("admin_service.main.build_runtime_payload", new=AsyncMock(return_value={})),
            patch("admin_service.main.serialize_systems", return_value=[]),
        ):
            response = asyncio.run(
                route.endpoint(
                    request,
                    stop_services=True,
                    restart_services=False,
                )
            )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(observed["content"], b"archive-bytes")
        self.assertEqual(observed["mode"], 0o600)
        archive_path = observed["path"]
        assert isinstance(archive_path, Path)
        self.assertFalse(archive_path.exists())
        self.assertFalse(archive_path.parent.exists())
        service.import_bundle_from_file.assert_called_once()
        service.import_bundle.assert_not_called()

    def test_admin_backup_import_without_stops_keeps_impacted_services_needing_restart(self) -> None:
        request, _receive_probe = make_streaming_request([b"archive-bytes"])
        maintenance = SimpleNamespace(
            stopped_containers=[],
            restarted_containers=[],
            restart_failures={},
        )
        maintenance_service = MagicMock()
        maintenance_service.import_bundle_from_file.return_value = (
            {
                "ok": True,
                "systems": [],
                "restored_paths": [],
                "preserved_absent_groups": [],
            },
            maintenance,
        )
        runtime_service = DockerRuntimeService(
            AdminSettings(docker_socket_path="/nonexistent.sock")
        )
        runtime_service.mark_restart_required(("ui",))
        route = next(
            route for route in admin_app.routes
            if route.path == "/api/admin/backup/import"
        )

        with (
            patch(
                "admin_service.main.get_maintenance_service",
                return_value=maintenance_service,
            ),
            patch(
                "admin_service.main.reload_app_settings",
                return_value=SimpleNamespace(default_system_id=None),
            ),
            patch(
                "admin_service.main.get_runtime_service",
                return_value=runtime_service,
            ),
            patch(
                "admin_service.main.build_runtime_payload",
                new=AsyncMock(return_value={}),
            ),
            patch("admin_service.main.serialize_systems", return_value=[]),
        ):
            asyncio.run(
                route.endpoint(
                    request,
                    stop_services=False,
                    restart_services=True,
                )
            )

        self.assertEqual(runtime_service.pending_restart_keys, {"ui", "history"})

    def test_admin_backup_export_cleans_workspace_when_response_setup_fails(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="admin-export-setup-failure-"))
        archive_path = workspace / "backup.zip"
        archive_path.write_bytes(b"PK-file-backed")
        artifact = FileBackupArtifact(
            filename="backup.zip",
            path=archive_path,
            media_type="application/zip",
            manifest={"packaging": "zip", "schema_version": 1},
            cleanup_root=workspace,
        )
        maintenance = SimpleNamespace(
            stopped_containers=[],
            restarted_containers=[],
            restart_failures={},
        )
        service = MagicMock()
        service.export_bundle.return_value = (artifact, maintenance)
        route = next(route for route in admin_app.routes if route.path == "/api/admin/backup/export")

        with (
            patch("admin_service.main.get_maintenance_service", return_value=service),
            patch(
                "admin_service.main.TemporaryFileResponse",
                side_effect=RuntimeError("response setup failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "response setup failed"):
                asyncio.run(
                    route.endpoint(
                        SystemBackupExportRequest(
                            packaging="zip",
                            encrypt=True,
                            passphrase="test-passphrase",
                        ),
                        stop_services=False,
                        restart_services=True,
                    )
                )

        self.assertFalse(workspace.exists())

    def test_admin_backup_export_cleans_workspace_when_request_is_cancelled(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="admin-export-cancelled-"))
        archive_path = workspace / "backup.zip"
        archive_path.write_bytes(b"PK-file-backed")
        artifact = FileBackupArtifact(
            filename="backup.zip",
            path=archive_path,
            media_type="application/zip",
            manifest={"packaging": "zip", "schema_version": 1},
            cleanup_root=workspace,
        )
        maintenance = SimpleNamespace(
            stopped_containers=[],
            restarted_containers=[],
            restart_failures={},
        )
        worker_started = threading.Event()
        release_worker = threading.Event()
        service = MagicMock()

        def export_bundle(*_: object, **__: object) -> tuple[FileBackupArtifact, object]:
            worker_started.set()
            release_worker.wait(timeout=1.0)
            return artifact, maintenance

        service.export_bundle.side_effect = export_bundle
        route = next(route for route in admin_app.routes if route.path == "/api/admin/backup/export")

        async def cancel_export() -> None:
            with patch("admin_service.main.get_maintenance_service", return_value=service):
                export_task = asyncio.create_task(
                    route.endpoint(
                        SystemBackupExportRequest(
                            packaging="zip",
                            encrypt=True,
                            passphrase="test-passphrase",
                        ),
                        stop_services=False,
                        restart_services=True,
                    )
                )
                started = await asyncio.to_thread(worker_started.wait, 1.0)
                self.assertTrue(started)
                export_task.cancel()
                release_worker.set()
                with self.assertRaises(asyncio.CancelledError):
                    await export_task

        asyncio.run(cancel_export())
        self.assertFalse(workspace.exists())

    def test_admin_backup_export_cleanup_survives_repeated_cancellation(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="admin-export-cancelled-twice-"))
        archive_path = workspace / "backup.zip"
        archive_path.write_bytes(b"PK-file-backed")
        artifact = FileBackupArtifact(
            filename="backup.zip",
            path=archive_path,
            media_type="application/zip",
            manifest={"packaging": "zip", "schema_version": 1},
            cleanup_root=workspace,
        )
        maintenance = SimpleNamespace(
            stopped_containers=[],
            restarted_containers=[],
            restart_failures={},
        )
        worker_started = threading.Event()
        release_worker = threading.Event()
        service = MagicMock()

        def export_bundle(*_: object, **__: object) -> tuple[FileBackupArtifact, object]:
            worker_started.set()
            release_worker.wait(timeout=1.0)
            return artifact, maintenance

        service.export_bundle.side_effect = export_bundle
        route = next(route for route in admin_app.routes if route.path == "/api/admin/backup/export")

        async def cancel_export_twice() -> None:
            with patch("admin_service.main.get_maintenance_service", return_value=service):
                export_task = asyncio.create_task(
                    route.endpoint(
                        SystemBackupExportRequest(
                            packaging="zip",
                            encrypt=True,
                            passphrase="test-passphrase",
                        ),
                        stop_services=False,
                        restart_services=True,
                    )
                )
                started = await asyncio.to_thread(worker_started.wait, 1.0)
                self.assertTrue(started)
                export_task.cancel()
                await asyncio.sleep(0)
                export_task.cancel()
                release_worker.set()
                with self.assertRaises(asyncio.CancelledError):
                    await export_task

        asyncio.run(cancel_export_twice())
        self.assertFalse(workspace.exists())

    def test_unhandled_exception_handlers_redact_exception_details(self) -> None:
        for app, port, expected_detail in (
            (main_app, 8080, "Unhandled application error; see application logs."),
            (admin_app, 8082, "Unhandled admin service error; see admin logs."),
        ):
            handler = app.exception_handlers[Exception]
            response = asyncio.run(
                handler(
                    make_request(port=port),
                    RuntimeError("Traceback: password=topsecret failure"),
                )
            )
            payload = json.loads(response.body.decode("utf-8"))

            self.assertEqual(response.status_code, 500)
            self.assertEqual(payload["detail"], expected_detail)
            joined = json.dumps(payload)
            self.assertNotIn("Traceback", joined)
            self.assertNotIn("topsecret", joined)

    def test_main_app_exposes_storage_view_runtime_route(self) -> None:
        paths = {route.path for route in main_app.routes}

        self.assertIn("/sas-fabric", paths)
        self.assertIn("/api/storage-views", paths)
        self.assertIn("/api/sas-fabric", paths)
        self.assertIn("/api/sas-fabric/aliases", paths)
        self.assertIn("/api/storage-views/{view_id}/slots/{slot_index}/history", paths)
        self.assertIn("/api/system-locator", paths)
        self.assertIn("/livez", paths)
        self.assertIn("/healthz", paths)

    def test_admin_and_history_services_expose_livez(self) -> None:
        admin_paths = {route.path for route in admin_app.routes}
        history_paths = {route.path for route in history_app.routes}

        self.assertIn("/livez", admin_paths)
        self.assertIn("/livez", history_paths)

    def test_main_ui_template_omits_storage_view_runtime_panel(self) -> None:
        template_path = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"
        template_text = template_path.read_text(encoding="utf-8")

        self.assertNotIn('id="storage-views-panel"', template_text)
        self.assertNotIn("Selected Storage View", template_text)

    def test_main_ui_template_includes_version_meta(self) -> None:
        template_path = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"
        template_text = template_path.read_text(encoding="utf-8")

        self.assertIn('id="app-version-value"', template_text)
        self.assertIn('id="app-version-note"', template_text)

    def test_main_ui_template_exposes_sas_fabric_panel(self) -> None:
        template_path = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"
        template_text = template_path.read_text(encoding="utf-8")

        self.assertIn('id="sas-fabric-toggle-button"', template_text)
        self.assertIn('id="sas-fabric-view-link"', template_text)
        self.assertIn('id="sas-fabric-panel"', template_text)
        self.assertIn("Fabric Inspector", template_text)

    def test_main_templates_allow_dedicated_sas_fabric_script(self) -> None:
        template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
        base_text = (template_dir / "base.html").read_text(encoding="utf-8")
        fabric_text = (template_dir / "sas_fabric.html").read_text(encoding="utf-8")

        self.assertIn("{% block scripts %}", base_text)
        self.assertIn("window.SAS_FABRIC_BOOTSTRAP", fabric_text)
        self.assertIn("sas_fabric_view.js", fabric_text)
        self.assertIn('data-fabric-mode="lanes"', fabric_text)
        self.assertIn('data-fabric-mode="impact"', fabric_text)
        self.assertIn('data-fabric-mode="trace"', fabric_text)
        self.assertIn('data-fabric-mode="disk"', fabric_text)
        self.assertIn('id="fabric-focus-strip"', fabric_text)
        self.assertIn('id="fabric-page-eyebrow"', fabric_text)
        self.assertIn('id="fabric-back-button"', fabric_text)
        self.assertNotIn('id="fabric-back-link"', fabric_text)

    def test_main_ui_template_exposes_refresh_timing_bootstrap(self) -> None:
        template_path = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"
        template_text = template_path.read_text(encoding="utf-8")

        self.assertIn('id="refresh-timing-strip"', template_text)
        self.assertIn('id="cache-timing-chips"', template_text)
        self.assertIn("snapshotCacheTtlSeconds", template_text)
        self.assertIn("sourceBundleCacheTtlSeconds", template_text)
        self.assertIn("smartCacheTtlSeconds", template_text)
        self.assertIn("sgSesDeviceCacheTtlSeconds", template_text)

    def test_admin_ui_template_includes_version_stat(self) -> None:
        template_path = Path(__file__).resolve().parents[1] / "admin_service" / "templates" / "index.html"
        template_text = template_path.read_text(encoding="utf-8")

        self.assertIn('id="admin-app-version"', template_text)
        self.assertIn('id="admin-release-note"', template_text)
        self.assertIn('<option value="none">Password Only / No Key</option>', template_text)
        self.assertIn('id="setup-esxi-host-prep-panel"', template_text)
        self.assertIn('id="setup-esxi-host-prep-package-select"', template_text)
        self.assertIn('id="setup-platform-requirements"', template_text)

    def test_main_ui_script_filters_admin_only_storage_views_from_selector(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn('view.render?.show_in_main_ui !== false', script_text)
        self.assertIn('get("storage_view_id")', script_text)
        self.assertIn('rawValue.startsWith("view:")', script_text)

    def test_main_ui_script_keeps_navigation_stale_first_with_background_led_verify(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn('await refreshSnapshot(false, "system-switch");', script_text)
        self.assertIn('await refreshSnapshot(false, "enclosure-switch");', script_text)
        self.assertIn('queueIdentifyVerify("startup");', script_text)
        self.assertIn('queueIdentifyVerify("system-switch");', script_text)
        self.assertIn('queueIdentifyVerify("enclosure-switch");', script_text)
        self.assertIn('void refreshSnapshot(true, `${reason}-led-verify`);', script_text)
        self.assertIn("snapshotMatchesSelectedSystem()", script_text)

    def test_main_ui_script_uses_bootstrap_refresh_timing(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn("const refreshTiming = bootstrap.refreshTiming || {};", script_text)
        self.assertIn("const SMART_SUMMARY_CACHE_TTL_MS = SMART_CACHE_TTL_SECONDS * 1000;", script_text)
        self.assertIn("renderCacheTimingChips", script_text)
        self.assertIn("data-cache-timing-key", script_text)
        self.assertNotIn("const SMART_SUMMARY_CACHE_TTL_MS = 5 * 60 * 1000;", script_text)

    def test_main_ui_script_wires_sas_fabric_render_surface(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn("/api/sas-fabric", script_text)
        self.assertIn("updateSasFabricViewLink", script_text)
        self.assertIn("renderSasFabric", script_text)
        self.assertIn("sasFabricSelectedSlotSet", script_text)
        self.assertIn("fabric-highlight", script_text)
        self.assertIn("data-sas-fabric-expand-slots", script_text)

    def test_dedicated_sas_fabric_script_wires_map_modes(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "sas_fabric_view.js"
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn("/api/inventory", script_text)
        self.assertIn("/api/sas-fabric", script_text)
        self.assertIn("renderLanesMode", script_text)
        self.assertIn("renderImpactMode", script_text)
        self.assertIn("renderTraceMode", script_text)
        self.assertIn("renderDiskPathMode", script_text)
        self.assertIn("sortFabricNodesForLane", script_text)
        self.assertIn("compareFabricTraceFlow", script_text)
        self.assertIn("fabricViewCopy", script_text)
        self.assertIn("linux_ses", script_text)
        self.assertIn("storage_quantastor", script_text)
        self.assertIn("storage_esxi", script_text)
        self.assertIn("Storage Lanes", script_text)
        self.assertIn("SES Link", script_text)
        self.assertIn("Quantastor HA-node", script_text)
        self.assertIn("Storage Fabric evidence", script_text)
        self.assertIn("numeric: true", script_text)
        self.assertIn("slotLayoutRows", script_text)
        self.assertIn("bestDiagnosticNode", script_text)
        self.assertIn("defaultSelectionRef", script_text)
        self.assertIn("data-fabric-mode-target", script_text)
        self.assertIn("Backplane Zone", script_text)
        self.assertIn("disk-path-board", script_text)
        self.assertIn("disk-path-bay-layout", script_text)
        self.assertIn("tooltipText", script_text)
        self.assertIn("expanderPhyForDevice", script_text)
        self.assertIn("IOC exceptions", script_text)
        self.assertIn("renderTraceBreadcrumbs", script_text)
        self.assertIn("relatedTracesForNode", script_text)
        self.assertIn("traceIsInSelectionTrail", script_text)
        self.assertIn("data-fabric-breadcrumb", script_text)
        self.assertIn("data-fabric-trace-disabled", script_text)
        self.assertIn("data-fabric-trace-home", script_text)
        self.assertIn('<span class="fabric-trace-index">1</span>', script_text)
        self.assertIn("renderDiagnosticTableControls", script_text)
        self.assertIn("data-fabric-diagnostic-page", script_text)
        self.assertIn("data-fabric-diagnostic-filter-key", script_text)
        self.assertIn("data-fabric-diagnostic-severity-key", script_text)
        self.assertIn("data-fabric-diagnostic-confidence-key", script_text)
        self.assertIn("Diagnostic event impact summary", script_text)
        self.assertIn("T10 standard", script_text)
        self.assertIn("data-fabric-alias-form", script_text)
        self.assertIn("data-fabric-alias-edit", script_text)
        self.assertIn("/api/sas-fabric/aliases", script_text)
        self.assertIn("Time / Order", script_text)
        self.assertIn("Filters apply only to this sample", script_text)
        self.assertIn("Previous event page", script_text)
        self.assertIn("PCI address", script_text)
        self.assertIn("PCIe slot", script_text)
        self.assertIn("Disk slot", script_text)
        self.assertIn('"decoded_records", "event_table"', script_text)
        self.assertNotIn("const decodedRows = list(diagnostics?.decoded_records)", script_text)
        self.assertIn('`${values.length} record${values.length === 1 ? "" : "s"}`', script_text)
        self.assertIn("Fabric Inspector", script_text)

    def test_dedicated_sas_fabric_disk_path_link_card_stays_local(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "sas_fabric_view.js"
        script_text = script_path.read_text(encoding="utf-8")
        start = script_text.index("function renderDiskPathBranch")
        end = script_text.index("function renderDiskPathMode")
        disk_path_branch = script_text[start:end]

        self.assertIn("kind: labels.path", disk_path_branch)
        self.assertIn("const pathNodeId =", disk_path_branch)
        self.assertIn("const pathTraceId =", disk_path_branch)
        self.assertIn("nodeId: pathNodeId", disk_path_branch)
        self.assertIn('traceId: pathNodeId ? "" : pathTraceId', disk_path_branch)
        self.assertNotIn('modeTarget: "impact"', disk_path_branch)

    def test_dedicated_sas_fabric_disk_path_keeps_active_bay_during_node_clicks(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "sas_fabric_view.js"
        script_text = script_path.read_text(encoding="utf-8")
        start = script_text.index("function selectedDiskTrace")
        end = script_text.index("function layoutSlotCount")
        selected_disk_trace = script_text[start:end]

        self.assertIn("selectedDiskTraceId", script_text)
        self.assertIn("function rememberDiskTrace", script_text)
        self.assertIn("function rememberedDiskTrace", script_text)
        self.assertIn("rememberDiskTrace(trace.id, fabric)", selected_disk_trace)
        self.assertIn("const rememberedTrace = rememberedDiskTrace(fabric)", selected_disk_trace)
        self.assertLess(
            selected_disk_trace.index("const rememberedTrace = rememberedDiskTrace(fabric)"),
            selected_disk_trace.index("const candidateSlots"),
        )

    def test_admin_script_disables_linux_bootstrap_flow_for_esxi(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "admin_service" / "static" / "admin.js"
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn("platformSupportsBootstrap", script_text)
        self.assertIn("setupPlatformUsesSshOnlyHost", script_text)
        self.assertIn("renderSetupPlatformRequirements", script_text)
        self.assertIn("setupPlatformRequirements", script_text)
        self.assertIn("Required", script_text)
        self.assertIn("Unsupported", script_text)
        self.assertIn("truenas_host: primaryHost", script_text)
        self.assertIn('value === "generate" || value === "manual" || value === "none"', script_text)
        self.assertIn('setupSshSudoPasswordField.classList.toggle("hidden", !savedSudoSupported)', script_text)
        self.assertIn("VMware ESXi does not use the one-time Linux service-account bootstrap.", script_text)
        self.assertIn("VMware ESXi does not use the Linux sudoers/bootstrap path.", script_text)
        self.assertIn("Password-only mode does not provide a service key for bootstrap.", script_text)
        self.assertIn("midclt user.update", script_text)
        self.assertIn("platformSupportsEsxiHostPrep", script_text)
        self.assertIn("/api/admin/esxi-host-prep/upload", script_text)
        self.assertIn("/api/admin/esxi-host-prep/install", script_text)
        self.assertIn("runtime-behavior-save-button", script_text)
        self.assertIn("/api/admin/runtime-behavior", script_text)
        self.assertIn("is-env-owned", script_text)

    def test_main_app_livez_is_lightweight(self) -> None:
        response = self._call_main_route("/livez")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["status"], "ok")
        self.assertEqual(json.loads(response.body)["version"], __version__)

    def test_admin_and_history_livez_report_shared_version(self) -> None:
        admin_route = next(route for route in admin_app.routes if route.path == "/livez")
        history_route = next(route for route in history_app.routes if route.path == "/livez")

        admin_response = asyncio.run(admin_route.endpoint())
        history_response = asyncio.run(history_route.endpoint())

        self.assertEqual(json.loads(admin_response.body)["version"], __version__)
        self.assertEqual(json.loads(history_response.body)["version"], __version__)

    def test_history_service_uses_shared_app_version(self) -> None:
        self.assertEqual(history_app.version, __version__)

    def test_main_app_healthz_uses_cached_snapshot_only(self) -> None:
        fake_service = MagicMock()
        fake_snapshot = MagicMock()
        fake_snapshot.sources = {"api": MagicMock(ok=True)}
        fake_snapshot.last_updated = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
        fake_snapshot.warnings = ["cached warning"]
        fake_snapshot.model_dump.return_value = {"sources": {"api": {"enabled": True, "ok": True, "message": "reachable"}}}
        fake_service.peek_cached_snapshot.return_value = fake_snapshot
        fake_registry = MagicMock()
        fake_registry.get_service.return_value = fake_service

        with patch("app.main.get_inventory_registry", return_value=fake_registry):
            response = self._call_main_route("/healthz")

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["dependency_status"], "ok")
        self.assertEqual(payload["cache_state"], "cached")
        fake_service.peek_cached_snapshot.assert_called_once_with()

    def test_main_app_healthz_reports_unknown_when_cache_is_empty(self) -> None:
        fake_service = MagicMock()
        fake_service.peek_cached_snapshot.return_value = None
        fake_registry = MagicMock()
        fake_registry.get_service.return_value = fake_service

        with patch("app.main.get_inventory_registry", return_value=fake_registry):
            response = self._call_main_route("/healthz")

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["dependency_status"], "unknown")
        self.assertEqual(payload["cache_state"], "empty")

    def test_snapshot_export_estimate_uses_stale_smart_cache(self) -> None:
        route = next(route for route in main_app.routes if route.path == "/api/export/enclosure-snapshot/estimate")
        snapshot = MagicMock()
        snapshot.slots = [SimpleNamespace(slot=0), SimpleNamespace(slot=1)]
        fake_summary = MagicMock()
        fake_summary.model_dump.return_value = {"available": True}
        fake_service = MagicMock()
        fake_service.system.id = "archive-core"
        fake_service.system.truenas.platform = "core"
        fake_service.get_snapshot = AsyncMock(return_value=snapshot)
        fake_service.get_slot_smart_summaries = AsyncMock(
            return_value=[SimpleNamespace(slot=0, summary=fake_summary)]
        )
        fake_registry = MagicMock()
        fake_registry.get_service.return_value = fake_service
        fake_exporter = MagicMock()
        fake_exporter.estimate_enclosure_snapshot_export = AsyncMock(return_value={"ok": True})

        with (
            patch("app.main.get_inventory_registry", return_value=fake_registry),
            patch("app.main.get_snapshot_export_service", return_value=fake_exporter),
        ):
            response = asyncio.run(
                route.endpoint(
                    make_request(port=8080),
                    SnapshotExportRequest(selected_slot=0),
                    system_id=None,
                    enclosure_id="front",
                )
            )

        self.assertEqual(response.status_code, 200)
        fake_service.get_slot_smart_summaries.assert_awaited_once_with(
            [0, 1],
            selected_enclosure_id="front",
            allow_stale_cache=True,
        )

    def test_snapshot_export_download_reuses_estimate_source_inputs_when_packaging_changes(self) -> None:
        estimate_route = next(route for route in main_app.routes if route.path == "/api/export/enclosure-snapshot/estimate")
        export_route = next(route for route in main_app.routes if route.path == "/api/export/enclosure-snapshot")
        snapshot = MagicMock()
        snapshot.slots = [SimpleNamespace(slot=0), SimpleNamespace(slot=1)]
        fake_summary = MagicMock()
        fake_summary.model_dump.return_value = {"available": True}
        fake_service = MagicMock()
        fake_service.system.id = "archive-core"
        fake_service.system.truenas.platform = "core"
        source_config = {
            "id": "archive-core",
            "truenas": {"host": "https://api206.route.invalid"},
            "ssh": {"host": "ssh206.route.invalid", "extra_hosts": [], "ha_nodes": []},
            "bmc": {"host": "bmc206.route.invalid"},
        }
        fake_service.system.model_dump.return_value = source_config
        fake_service.get_snapshot = AsyncMock(return_value=snapshot)
        fake_service.get_slot_smart_summaries = AsyncMock(
            return_value=[SimpleNamespace(slot=0, summary=fake_summary)]
        )
        fake_registry = MagicMock()
        fake_registry.get_service.return_value = fake_service
        fake_exporter = MagicMock()
        fake_exporter.estimate_enclosure_snapshot_export = AsyncMock(return_value={"ok": True})
        fake_exporter.build_enclosure_snapshot_export = AsyncMock(
            return_value=PackagedSnapshotExport(
                filename="snapshot.zip",
                content=b"zip",
                media_type="application/zip",
                size_bytes=3,
                html_size_bytes=12,
                packaging="zip",
                redaction="full",
                size_limit_bytes=24 * 1024 * 1024,
            )
        )

        with (
            patch("app.main.get_inventory_registry", return_value=fake_registry),
            patch("app.main.get_snapshot_export_service", return_value=fake_exporter),
        ):
            estimate_response = asyncio.run(
                estimate_route.endpoint(
                    make_request(port=8080),
                    SnapshotExportRequest(
                        selected_slot=0,
                        history_window_hours=168,
                        history_panel_open=True,
                        io_chart_mode="total",
                        packaging="auto",
                    ),
                    system_id=None,
                    enclosure_id="front",
                )
            )
            export_response = asyncio.run(
                export_route.endpoint(
                    make_request(port=8080),
                    SnapshotExportRequest(
                        selected_slot=0,
                        history_window_hours=168,
                        history_panel_open=True,
                        io_chart_mode="total",
                        packaging="zip",
                    ),
                    system_id=None,
                    enclosure_id="front",
                )
            )

        self.assertEqual(estimate_response.status_code, 200)
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response.headers["X-Export-Packaging"], "zip")
        self.assertEqual(
            fake_exporter.estimate_enclosure_snapshot_export.await_args.kwargs.get("configured_hostnames"),
            ["api206.route.invalid", "ssh206.route.invalid", "bmc206.route.invalid"],
        )
        self.assertEqual(
            fake_exporter.build_enclosure_snapshot_export.await_args.kwargs.get("configured_hostnames"),
            ["api206.route.invalid", "ssh206.route.invalid", "bmc206.route.invalid"],
        )
        self.assertNotIn("source_config", fake_exporter.estimate_enclosure_snapshot_export.await_args.kwargs)
        self.assertNotIn("source_config", fake_exporter.build_enclosure_snapshot_export.await_args.kwargs)
        self.assertEqual(fake_service.system.model_dump.call_count, 2)
        fake_service.system.model_dump.assert_called_with(mode="json")
        fake_service.get_snapshot.assert_awaited_once_with(selected_enclosure_id="front")
        fake_service.get_slot_smart_summaries.assert_awaited_once_with(
            [0, 1],
            selected_enclosure_id="front",
            allow_stale_cache=True,
        )

    def test_resolve_admin_launch_url_returns_public_url_when_sidecar_is_healthy(self) -> None:
        request = make_request(port=8080)
        settings = Settings(
            admin=AdminSurfaceConfig(
                service_url="http://enclosure-admin:8002",
                public_url="http://127.0.0.1:8082",
                port=8082,
                timeout_seconds=0.5,
            )
        )
        response = MagicMock()
        response.__enter__.return_value.status = 200

        with patch("app.main.urllib.request.urlopen", return_value=response):
            launch_url = resolve_admin_launch_url(request, settings)

        self.assertEqual(launch_url, "http://127.0.0.1:8082")

    def test_resolve_admin_launch_url_hides_button_when_sidecar_is_down(self) -> None:
        request = make_request(port=8080)
        settings = Settings(
            admin=AdminSurfaceConfig(
                service_url="http://enclosure-admin:8002",
                public_url="http://127.0.0.1:8082",
                port=8082,
                timeout_seconds=0.5,
            )
        )

        with patch(
            "app.main.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            launch_url = resolve_admin_launch_url(request, settings)

        self.assertIsNone(launch_url)

    def test_resolve_admin_launch_url_hides_button_when_sidecar_times_out(self) -> None:
        request = make_request(port=8080)
        settings = Settings(
            admin=AdminSurfaceConfig(
                service_url="http://enclosure-admin:8002",
                public_url="http://127.0.0.1:8082",
                port=8082,
                timeout_seconds=0.5,
            )
        )

        with patch(
            "app.main.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            launch_url = resolve_admin_launch_url(request, settings)

        self.assertIsNone(launch_url)


class AdminHeaderDecodeTests(unittest.TestCase):
    def test_decode_optional_secret_header_preserves_trailing_spaces(self) -> None:
        encoded = "cGFkZGVkIHNlY3JldCAgIA=="

        decoded = decode_optional_secret_header(encoded)

        self.assertEqual(decoded, "padded secret   ")

    def test_decode_optional_secret_header_rejects_invalid_base64(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid base64"):
            decode_optional_secret_header("not base64!!!")


class AdminStatePayloadTests(unittest.TestCase):
    def test_build_admin_state_payload_includes_profile_defaults_and_public_origin(self) -> None:
        settings = Settings(
            config_file="C:/tmp/config/config.yaml",
            paths=PathConfig(
                runtime_overrides_file="C:/tmp/config/runtime-overrides.yaml",
                mapping_file="C:/tmp/data/slot_mappings.json",
                log_file="C:/tmp/logs/app.log",
                profile_file="C:/tmp/config/profiles.yaml",
                slot_detail_cache_file="C:/tmp/data/slot_detail_cache.json",
            ),
            systems=[
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    default_profile_id="lab-4x4",
                    storage_views=[
                        {
                            "id": "front-bays",
                            "label": "Front Bays",
                            "kind": "ses_enclosure",
                            "template_id": "ses-auto",
                            "profile_id": "lab-4x4",
                            "enabled": True,
                            "order": 10,
                            "render": {
                                "show_in_main_ui": True,
                                "show_in_admin_ui": True,
                                "default_collapsed": False,
                            },
                            "binding": {
                                "mode": "auto",
                                "enclosure_ids": ["enc-a"],
                                "pool_names": [],
                                "serials": [],
                                "pcie_addresses": [],
                                "device_names": [],
                            },
                        }
                    ],
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        api_key="API-KEY-1",
                        verify_ssl=False,
                        tls_ca_bundle_path="/app/config/tls/archive-core.pem",
                        tls_server_name="TrueNAS.gcs8.io",
                        enclosure_filter="front",
                    ),
                    ssh=SSHConfig(
                        enabled=True,
                        host="archive-core.local",
                        user="jbodmap",
                        key_path="/run/ssh/id_truenas",
                        commands=["/usr/sbin/zpool status -gP"],
                    ),
                )
            ],
            default_system_id="archive-core",
            profiles=[
                {
                    "id": "lab-4x4",
                    "label": "Lab 4x4",
                    "summary": "Compact 16-bay lab mockup.",
                    "rows": 4,
                    "columns": 4,
                    "slot_layout": [
                        [0, 1, 2, 3],
                        [4, 5, 6, 7],
                        [8, 9, 10, 11],
                        [12, 13, 14, 15],
                    ],
                }
            ],
        )
        request = make_request(port=8082)
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {
            "available": True,
            "detail": None,
            "containers": [
                {
                    "key": "ui",
                    "label": "Read UI",
                    "status": "running",
                    "status_text": "Up 2 minutes (healthy)",
                    "running": True,
                    "can_stop": True,
                    "can_start": False,
                }
            ],
        }
        key_manager = MagicMock()
        key_manager.list_keys.return_value = [
            {
                "name": "id_truenas",
                "runtime_private_path": "/run/ssh/id_truenas",
                "fingerprint": "SHA256:abc123",
                "algorithm": "ed25519",
            }
        ]
        host_prep_service = MagicMock()
        host_prep_service.list_staged_packages.return_value = [
            {
                "token": "storcli-1",
                "filename": "BCM-vmware-storcli64.zip",
                "extension": ".zip",
                "install_mode": "component_bundle",
                "size_bytes": 4096,
                "created_at": "2026-04-26T20:00:00+00:00",
                "staged_path": "/tmp/truenas-jbod-ui-host-prep/storcli-1/BCM-vmware-storcli64.zip",
            }
        ]

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch("admin_service.main.get_esxi_host_prep_service", return_value=host_prep_service):
                        with patch(
                            "admin_service.main.get_admin_settings",
                            return_value=AdminSettings(
                                auto_stop_seconds=3600,
                                host_prep_temp_dir="/tmp/truenas-jbod-ui-host-prep",
                                allow_plaintext_backup_export=True,
                            ),
                        ):
                            with patch(
                                "admin_service.main.get_history_settings",
                                return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                            ):
                                payload = asyncio.run(build_admin_state_payload(request))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app_version"], __version__)
        self.assertEqual(payload["admin"]["public_origin"], "http://localhost:8082")
        self.assertEqual(payload["default_system_id"], "archive-core")
        self.assertEqual(payload["systems"][0]["truenas_host"], "https://archive-core.local")
        self.assertFalse(payload["systems"][0]["verify_ssl"])
        self.assertEqual(payload["systems"][0]["tls_ca_bundle_path"], "/app/config/tls/archive-core.pem")
        self.assertEqual(payload["systems"][0]["tls_server_name"], "TrueNAS.gcs8.io")
        self.assertTrue(payload["systems"][0]["ssh_enabled"])
        self.assertFalse(payload["systems"][0]["bmc_enabled"])
        self.assertEqual(payload["systems"][0]["ssh_key_path"], "/run/ssh/id_truenas")
        self.assertEqual(payload["systems"][0]["storage_views"][0]["id"], "front-bays")
        self.assertEqual(payload["systems"][0]["storage_views"][0]["template_id"], "ses-auto")
        self.assertEqual(payload["systems"][0]["storage_views"][0]["profile_id"], "lab-4x4")
        self.assertEqual(payload["storage_view_templates"][0]["id"], "ses-auto")
        self.assertTrue(any(template["id"] == "aoc-slg4-2h8m2-2" for template in payload["storage_view_templates"]))
        self.assertIn("runtime_behavior", payload)
        self.assertTrue(any(field["key"] == "source_bundle_cache_ttl_seconds" for field in payload["runtime_behavior"]["fields"]))
        custom_profile = next(profile for profile in payload["profiles"] if profile["id"] == "lab-4x4")
        self.assertEqual(custom_profile["slot_count"], 16)
        self.assertTrue(custom_profile["is_custom"])
        self.assertEqual(custom_profile["reference_count"], 2)
        self.assertIn("core", payload["setup_platform_defaults"])
        self.assertIn(
            "sudo -n /usr/sbin/mprutil show adapters",
            payload["setup_platform_defaults"]["core"]["ssh_commands"],
        )
        self.assertIn("/usr/sbin/pciconf -lv", payload["setup_platform_defaults"]["core"]["ssh_commands"])
        self.assertIn(
            "sysctl -a 2>/dev/null | egrep '^dev\\.mpr\\.[0-9]+\\.%(location|parent):' || true",
            payload["setup_platform_defaults"]["core"]["ssh_commands"],
        )
        self.assertIn(
            "sudo -n /usr/local/sbin/dmidecode -t slot",
            payload["setup_platform_defaults"]["core"]["ssh_commands"],
        )
        self.assertTrue(
            any("/var/log/messages" in command for command in payload["setup_platform_defaults"]["core"]["ssh_commands"])
        )
        scale_requirements = payload["setup_platform_defaults"]["scale"]["requirements"]
        self.assertIn("/usr/bin/lsscsi -g -t", scale_requirements["required"][1])
        self.assertIn("/usr/bin/lsblk --json", scale_requirements["required"][1])
        self.assertTrue(any("/dev/sgN" in item for item in scale_requirements["optional"]))
        self.assertTrue(any("nvme-cli" in item for item in scale_requirements["optional"]))
        self.assertTrue(any("sesutil" in item for item in scale_requirements["unsupported"]))
        self.assertTrue(any("mprutil" in item for item in scale_requirements["unsupported"]))
        linux_requirements = payload["setup_platform_defaults"]["linux"]["requirements"]
        self.assertTrue(any("lsblk --json" in item for item in linux_requirements["required"]))
        self.assertIn("lsscsi -g -t", linux_requirements["guidance"])
        esxi_requirements = payload["setup_platform_defaults"]["esxi"]["requirements"]
        self.assertTrue(any("Linux sudoers/bootstrap" in item for item in esxi_requirements["unsupported"]))
        self.assertIn("/cN or /call", esxi_requirements["guidance"])
        self.assertIn("esxi", payload["setup_platform_defaults"])
        self.assertIn("ipmi", payload["setup_platform_defaults"])
        self.assertEqual(payload["ssh_keys"][0]["name"], "id_truenas")
        self.assertEqual(payload["esxi_host_prep"]["temp_dir"], "/tmp/truenas-jbod-ui-host-prep")
        self.assertEqual(payload["esxi_host_prep"]["staged_packages"][0]["filename"], "BCM-vmware-storcli64.zip")
        self.assertEqual(payload["paths"]["history_db"], "/tmp/history/history.db")
        self.assertEqual(payload["paths"]["runtime_overrides_file"], "C:/tmp/config/runtime-overrides.yaml")
        self.assertEqual(payload["paths"]["tls_dir"], str(Path("C:/tmp/config") / "tls"))
        self.assertIn("included_paths", payload["backup_defaults"])
        self.assertIn("debug_included_paths", payload["backup_defaults"])
        self.assertIn("runtime_overrides_file", payload["backup_defaults"]["included_paths"])
        self.assertTrue(payload["backup_defaults"]["debug_scrub_secrets"])
        self.assertTrue(payload["backup_defaults"]["debug_scrub_disk_identifiers"])
        self.assertTrue(payload["backup_defaults"]["allow_plaintext_backup_export"])
        self.assertTrue(any(group["key"] == "ssh_keys" for group in payload["backup_defaults"]["path_groups"]))

    def test_build_admin_state_payload_includes_release_status(self) -> None:
        request = make_request(port=8082)
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": False, "detail": None, "containers": []}
        key_manager = MagicMock()
        key_manager.list_keys.return_value = []
        release_service = MagicMock()
        release_service.snapshot.return_value = {
            "current_version": __version__,
            "status": "dev-build",
            "summary": "Dev build · latest stable v0.14.1",
            "latest_tag": "v0.14.1",
            "latest_url": "https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.14.1",
        }

        with patch("admin_service.main.reload_app_settings", return_value=Settings()):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch("admin_service.main.get_release_status_service", return_value=release_service):
                        with patch(
                            "admin_service.main.get_admin_settings",
                            return_value=AdminSettings(auto_stop_seconds=3600),
                        ):
                            with patch(
                                "admin_service.main.get_history_settings",
                                return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                            ):
                                payload = asyncio.run(build_admin_state_payload(request))

        self.assertEqual(payload["release_status"]["status"], "dev-build")
        self.assertEqual(payload["release_status"]["latest_tag"], "v0.14.1")

    def test_build_admin_state_payload_redacts_saved_secret_fields(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="esxi-ft-node-2",
                    label="esxi-ft-node-2",
                    truenas=TrueNASConfig(
                        host="192.0.2.121",
                        api_key=MARKER_ALPHA,
                        api_user="root",
                        api_password=MARKER_BRAVO,
                        platform="esxi",
                        verify_ssl=False,
                    ),
                    ssh=SSHConfig(
                        enabled=True,
                        host="192.0.2.121",
                        user="root",
                        key_path="",
                        password=MARKER_CHARLIE,
                        sudo_password=MARKER_DELTA,
                        strict_host_key_checking=False,
                        commands=["vmware -v"],
                    ),
                    bmc=BMCConfig(
                        enabled=True,
                        host="192.0.2.200",
                        username="ADMIN",
                        password=MARKER_ECHO,
                    ),
                )
            ],
            default_system_id="esxi-ft-node-2",
        )
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": False, "detail": None, "containers": []}
        key_manager = MagicMock()
        key_manager.list_keys.return_value = []

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch("admin_service.main.get_admin_settings", return_value=AdminSettings()):
                        with patch(
                            "admin_service.main.get_history_settings",
                            return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                        ):
                            payload = asyncio.run(build_admin_state_payload(make_request(port=8082)))

        saved_system = payload["systems"][0]
        self.assertEqual(saved_system["truenas_host"], "192.0.2.121")
        self.assertEqual(saved_system["ssh_host"], "192.0.2.121")
        self.assertEqual(saved_system["ssh_key_path"], "")
        self.assertEqual(saved_system["api_key"], "")
        self.assertTrue(saved_system["api_key_configured"])
        self.assertEqual(saved_system["api_password"], "")
        self.assertTrue(saved_system["api_password_configured"])
        self.assertEqual(saved_system["ssh_password"], "")
        self.assertTrue(saved_system["ssh_password_configured"])
        self.assertEqual(saved_system["ssh_sudo_password"], "")
        self.assertTrue(saved_system["ssh_sudo_password_configured"])
        self.assertEqual(saved_system["bmc_password"], "")
        self.assertTrue(saved_system["bmc_password_configured"])
        rendered_payload = json.dumps(payload)
        self.assertNotIn(MARKER_ALPHA, rendered_payload)
        self.assertNotIn(MARKER_BRAVO, rendered_payload)
        self.assertNotIn(MARKER_CHARLIE, rendered_payload)
        self.assertNotIn(MARKER_DELTA, rendered_payload)
        self.assertNotIn(MARKER_ECHO, rendered_payload)

    def test_admin_bootstrap_template_escapes_script_breakout(self) -> None:
        request = make_request(port=8082)
        request.scope["router"] = admin_app.router
        rendered = admin_templates.get_template("index.html").render(
            request=request,
            admin_bootstrap_json=json.dumps(
                {
                    "ok": True,
                    "systems": [
                        {
                            "id": "script-breakout",
                            "label": "</script><script>window.__admin_xss = true;</script>",
                        }
                    ],
                }
            ),
        )

        self.assertIn("\\u003c/script\\u003e", rendered)
        self.assertNotIn("</script><script>window.__admin_xss", rendered)

    def test_admin_js_avoids_dom_reinterpretation_for_code_scanning_surfaces(self) -> None:
        admin_js = (
            Path(__file__).resolve().parents[1]
            / "admin_service"
            / "static"
            / "admin.js"
        ).read_text(encoding="utf-8")

        self.assertNotIn("elements.releaseNote.innerHTML", admin_js)
        self.assertNotIn("elements.runtimeCards.innerHTML", admin_js)
        self.assertIn("function safeHttpUrl(value)", admin_js)
        self.assertIn("const latestUrl = safeHttpUrl(releaseStatus.latest_url);", admin_js)
        self.assertNotIn("const latestUrl = String(releaseStatus.latest_url || \"\").trim();", admin_js)

    def test_sas_fabric_compact_labels_do_not_replace_dev_prefix_with_itself(self) -> None:
        sas_js = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "static"
            / "sas_fabric_view.js"
        ).read_text(encoding="utf-8")

        self.assertNotIn('.replace(/^\\/dev\\//, "/dev/")', sas_js)

    def test_admin_js_preserve_sentinel_is_limited_to_saved_system_flows(self) -> None:
        admin_js = (
            Path(__file__).resolve().parents[1]
            / "admin_service"
            / "static"
            / "admin.js"
        ).read_text(encoding="utf-8")

        self.assertEqual(admin_js.count("preserveRedactedSecrets: true"), 3)
        self.assertIn(
            "const payload = collectSetupPayload({ preserveRedactedSecrets: true });",
            admin_js,
        )
        self.assertIn(
            "const setupPayload = collectSetupPayload({ preserveRedactedSecrets: true });",
            admin_js,
        )

    def test_quantastor_ssh_enrichment_redacts_exception_details_from_result(self) -> None:
        payload = QuantastorNodeDiscoveryRequest(
            truenas_host="https://qs.example.test",
            api_user="qsadmin",
            api_password="api-secret",
            ssh_enabled=True,
            ssh_host="192.0.2.10",
            ssh_user="root",
            ssh_password="ssh-secret",
        )
        raw_data = TrueNASRawData(
            enclosures=[],
            disks=[],
            pools=[],
            disk_temperatures={},
            smart_test_results=[],
        )
        nodes = [{"id": "node-a", "label": "Node A", "host": ""}]

        with patch(
            "admin_service.main.SSHProbe.run_commands",
            new=AsyncMock(side_effect=RuntimeError("Traceback: password=ssh-secret timed out")),
        ):
            result = asyncio.run(enrich_quantastor_nodes_from_ssh(payload, raw_data, nodes))

        self.assertFalse(result["ok"])
        self.assertIn("failures", result)
        joined = json.dumps(result)
        self.assertNotIn("Traceback", joined)
        self.assertNotIn("ssh-secret", joined)
        self.assertIn("see admin logs", joined)

    def test_annotate_runtime_versions_marks_out_of_sync_services(self) -> None:
        runtime_payload = {
            "available": True,
            "detail": None,
            "containers": [
                {"key": "ui", "running": True, "running_version": "0.14.1"},
                {"key": "history", "running": True, "running_version": "0.15.0-dev"},
                {"key": "admin", "running": True, "running_version": "0.15.0-dev"},
            ],
        }
        release_status = {
            "status": "dev-build",
            "summary": "Dev build · latest stable v0.14.1",
            "latest_tag": "v0.14.1",
        }

        annotated = annotate_runtime_versions(runtime_payload, release_status)

        ui_container = next(item for item in annotated["containers"] if item["key"] == "ui")
        history_container = next(item for item in annotated["containers"] if item["key"] == "history")
        self.assertEqual(ui_container["latest_version"], "v0.14.1")
        self.assertEqual(ui_container["release_status"]["status"], "current")
        self.assertEqual(ui_container["version_sync_state"], "out_of_sync")
        self.assertIn("0.15.0-dev", ui_container["version_sync_summary"])
        self.assertEqual(history_container["release_status"]["status"], "dev-build")
        self.assertEqual(history_container["version_sync_state"], "out_of_sync")

    def test_annotate_runtime_versions_handles_probe_failures(self) -> None:
        runtime_payload = {
            "available": True,
            "detail": None,
            "containers": [
                {
                    "key": "history",
                    "running": True,
                    "running_version": None,
                    "version_probe_error": "Version probe failed for http://enclosure-history:8001/livez: timed out.",
                }
            ],
        }
        release_status = {
            "status": "current",
            "summary": "Latest tagged release",
            "latest_tag": "v0.14.1",
        }

        annotated = annotate_runtime_versions(runtime_payload, release_status)

        history_container = annotated["containers"][0]
        self.assertEqual(history_container["release_status"]["status"], "known")
        self.assertEqual(history_container["version_sync_state"], "unknown")
        self.assertIn("Version probe failed", history_container["version_sync_summary"])

    def test_build_admin_state_payload_includes_quantastor_ha_nodes(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="example-qs-ha",
                    label="ExampleQS HA",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        api_user="jbodmap",
                        api_password="secret",
                        platform="quantastor",
                        verify_ssl=False,
                    ),
                    ssh=SSHConfig(
                        enabled=True,
                        host="192.0.2.30",
                        extra_hosts=["192.0.2.31"],
                        ha_enabled=True,
                        ha_nodes=[
                            {
                                "system_id": "node-a",
                                "label": "ExampleQS Left",
                                "host": "192.0.2.30",
                            },
                            {
                                "system_id": "node-b",
                                "label": "ExampleQS Right",
                                "host": "192.0.2.31",
                            },
                        ],
                        user="jbodmap",
                        key_path="/run/ssh/id_truenas",
                    ),
                    storage_views=[
                        {
                            "id": "boot-doms-b",
                            "label": "Boot SATADOMs B",
                            "kind": "boot_devices",
                            "template_id": "satadom-pair-2",
                            "enabled": True,
                            "order": 30,
                            "render": {
                                "show_in_main_ui": True,
                                "show_in_admin_ui": True,
                                "default_collapsed": False,
                            },
                            "binding": {
                                "mode": "hybrid",
                                "target_system_id": "node-b",
                                "enclosure_ids": [],
                                "pool_names": ["ExampleQS-BOOT-B"],
                                "serials": [],
                                "pcie_addresses": [],
                                "device_names": ["sda", "sdb"],
                            },
                        }
                    ],
                )
            ],
            default_system_id="example-qs-ha",
        )
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}
        key_manager = MagicMock()
        key_manager.list_keys.return_value = []

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch("admin_service.main.get_admin_settings", return_value=AdminSettings()):
                        with patch(
                            "admin_service.main.get_history_settings",
                            return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                        ):
                            payload = asyncio.run(build_admin_state_payload(make_request(port=8082)))

        self.assertTrue(payload["systems"][0]["ha_enabled"])
        self.assertEqual(payload["systems"][0]["ha_nodes"][0]["system_id"], "node-a")
        self.assertEqual(payload["systems"][0]["ha_nodes"][1]["host"], "192.0.2.31")
        self.assertEqual(
            payload["systems"][0]["storage_views"][0]["binding"]["target_system_id"],
            "node-b",
        )

    def test_build_admin_state_payload_seeds_primary_chassis_view_for_auto_profile_legacy_systems(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="legacy-core",
                    label="Legacy CORE",
                    truenas=TrueNASConfig(
                        host="https://legacy-core.local",
                        platform="core",
                    ),
                )
            ],
            default_system_id="legacy-core",
        )

        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}
        key_manager = MagicMock()
        key_manager.list_keys.return_value = []

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch("admin_service.main.get_admin_settings", return_value=AdminSettings()):
                        with patch(
                            "admin_service.main.get_history_settings",
                            return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                        ):
                            payload = asyncio.run(build_admin_state_payload(make_request(port=8082)))

        inferred_view = payload["systems"][0]["storage_views"][0]
        self.assertEqual(inferred_view["id"], "primary-chassis")
        self.assertEqual(inferred_view["template_id"], "ses-auto")
        self.assertEqual(inferred_view["kind"], "ses_enclosure")
        self.assertEqual(inferred_view["profile_id"], "supermicro-cse-946-top-60")
        self.assertTrue(inferred_view["render"]["show_in_main_ui"])

    def test_build_admin_state_payload_infers_unifi_embedded_boot_media_view(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="unvr",
                    label="UniFi UNVR",
                    default_profile_id=UNIFI_UNVR_FRONT_4_PROFILE_ID,
                    truenas=TrueNASConfig(
                        host="https://unvr.local",
                        platform="linux",
                    ),
                    ssh=SSHConfig(
                        enabled=True,
                        host="unvr.local",
                        user="root",
                    ),
                )
            ],
            default_system_id="unvr",
        )

        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}
        key_manager = MagicMock()
        key_manager.list_keys.return_value = []

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch("admin_service.main.get_admin_settings", return_value=AdminSettings()):
                        with patch(
                            "admin_service.main.get_history_settings",
                            return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                        ):
                            payload = asyncio.run(build_admin_state_payload(make_request(port=8082)))

        views = payload["systems"][0]["storage_views"]
        self.assertEqual([view["id"] for view in views], ["primary-chassis", "embedded-boot-media"])
        boot_view = next(view for view in views if view["id"] == "embedded-boot-media")
        self.assertEqual(boot_view["template_id"], "embedded-boot-media-1")
        self.assertEqual(boot_view["binding"]["device_names"], ["boot"])
        self.assertTrue(any(template["id"] == "embedded-boot-media-1" for template in payload["storage_view_templates"]))

    def test_build_admin_state_payload_prefers_saved_storage_views_over_seeded_chassis(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="legacy-core",
                    label="Legacy CORE",
                    truenas=TrueNASConfig(
                        host="https://legacy-core.local",
                        platform="core",
                    ),
                    storage_views=[
                        {
                            "id": "nvme-card",
                            "label": "4x NVMe Carrier",
                            "kind": "nvme_carrier",
                            "template_id": "nvme-carrier-4",
                            "enabled": True,
                            "order": 20,
                            "render": {
                                "show_in_main_ui": True,
                                "show_in_admin_ui": True,
                                "default_collapsed": False,
                            },
                            "binding": {
                                "mode": "auto",
                                "enclosure_ids": [],
                                "pool_names": [],
                                "serials": [],
                                "pcie_addresses": [],
                                "device_names": [],
                            },
                        }
                    ],
                )
            ],
            default_system_id="legacy-core",
        )

        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}
        key_manager = MagicMock()
        key_manager.list_keys.return_value = []

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch("admin_service.main.get_admin_settings", return_value=AdminSettings()):
                        with patch(
                            "admin_service.main.get_history_settings",
                            return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                        ):
                            payload = asyncio.run(build_admin_state_payload(make_request(port=8082)))

        views = payload["systems"][0]["storage_views"]
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["id"], "nvme-card")
        self.assertEqual(views[0]["template_id"], "nvme-carrier-4")


class AdminSudoPreviewRouteTests(unittest.TestCase):
    def test_runtime_behavior_route_marks_read_ui_restart(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/runtime-behavior")
        settings = Settings(config_file="C:/tmp/config/config.yaml")
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {
            "available": True,
            "detail": None,
            "containers": [],
        }

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch(
                    "admin_service.main.save_runtime_behavior_overrides",
                    return_value={"fields": [{"key": "source_bundle_cache_ttl_seconds", "owner": "admin"}]},
                ) as save_overrides:
                    with patch(
                        "admin_service.main.build_runtime_payload",
                        new=AsyncMock(return_value={"available": True, "containers": []}),
                    ):
                        response = asyncio.run(
                            route.endpoint({"values": {"source_bundle_cache_ttl_seconds": 120}})
                        )

        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["runtime_behavior"]["fields"][0]["key"], "source_bundle_cache_ttl_seconds")
        save_overrides.assert_called_once_with(settings, {"source_bundle_cache_ttl_seconds": 120})
        runtime_service.mark_restart_required.assert_called_once_with(("ui",))

    def test_create_demo_system_route_accepts_missing_payload_and_marks_ui_restart(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/demo")
        initial_settings = Settings(
            config_file="C:/tmp/config/config.yaml",
            paths=PathConfig(
                mapping_file="C:/tmp/data/slot_mappings.json",
                log_file="C:/tmp/logs/app.log",
                profile_file="C:/tmp/config/profiles.yaml",
                slot_detail_cache_file="C:/tmp/data/slot_detail_cache.json",
            ),
        )
        refreshed_settings = Settings(
            config_file="C:/tmp/config/config.yaml",
            paths=PathConfig(
                mapping_file="C:/tmp/data/slot_mappings.json",
                log_file="C:/tmp/logs/app.log",
                profile_file="C:/tmp/config/profiles.yaml",
                slot_detail_cache_file="C:/tmp/data/slot_detail_cache.json",
            ),
            systems=[
                SystemConfig(
                    id="demo-builder-lab",
                    label="Demo Builder Lab",
                    default_profile_id="demo-builder-lab-chassis",
                    truenas=TrueNASConfig(
                        host="https://demo-builder.invalid",
                        platform="linux",
                    ),
                )
            ],
            profiles=[
                EnclosureProfileConfig(
                    id="demo-builder-lab-chassis",
                    label="Demo Builder Lab Chassis",
                    summary="Synthetic demo profile.",
                    face_style="front-drive",
                    latch_edge="top",
                    bay_size="2.5",
                    rows=3,
                    columns=4,
                    slot_layout=[[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]],
                )
            ],
        )
        demo_factory = MagicMock()
        demo_factory.create_demo_system.return_value = {
            "system": refreshed_settings.systems[0],
            "profile": refreshed_settings.profiles[0],
            "updated_existing": False,
            "updated_profile": False,
        }
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}

        with patch("admin_service.main.reload_app_settings", side_effect=[initial_settings, refreshed_settings]):
            with patch("admin_service.main.DemoSystemFactory", return_value=demo_factory):
                with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                    response = asyncio.run(route.endpoint())

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["system"]["id"], "demo-builder-lab")
        self.assertEqual(payload["profile"]["id"], "demo-builder-lab-chassis")
        runtime_service.mark_restart_required.assert_called_once_with(("ui",))

    def test_delete_system_route_returns_updated_system_list(self) -> None:
        route = next(
            route for route in admin_app.routes
            if route.path == "/api/admin/system-setup/{system_id}" and "DELETE" in getattr(route, "methods", set())
        )
        initial_settings = Settings(
            systems=[
                SystemConfig(
                    id="qs-cryostorage",
                    label="QS CryoStorage",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        platform="quantastor",
                    ),
                ),
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        platform="core",
                    ),
                ),
            ],
            default_system_id="qs-cryostorage",
        )
        refreshed_settings = Settings(
            systems=[
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        platform="core",
                    ),
                )
            ],
            default_system_id="archive-core",
        )
        setup_service = MagicMock()
        setup_service.delete_system.return_value = ("QS CryoStorage", "archive-core")
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}

        with patch("admin_service.main.reload_app_settings", side_effect=[initial_settings, refreshed_settings]):
            with patch("admin_service.main.SystemSetupService", return_value=setup_service):
                with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                    response = asyncio.run(route.endpoint(system_id="qs-cryostorage"))

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["system_id"], "qs-cryostorage")
        self.assertEqual(payload["deleted_label"], "QS CryoStorage")
        self.assertEqual(payload["default_system_id"], "archive-core")
        self.assertFalse(payload["history_purge"]["requested"])
        self.assertEqual([system["id"] for system in payload["systems"]], ["archive-core"])
        runtime_service.mark_restart_required.assert_called_once_with(("ui",))

    def test_delete_system_route_can_purge_matching_history(self) -> None:
        route = next(
            route for route in admin_app.routes
            if route.path == "/api/admin/system-setup/{system_id}" and "DELETE" in getattr(route, "methods", set())
        )
        initial_settings = Settings(
            systems=[
                SystemConfig(
                    id="qs-cryostorage",
                    label="QS CryoStorage",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        platform="quantastor",
                    ),
                ),
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        platform="core",
                    ),
                ),
            ],
            default_system_id="qs-cryostorage",
        )
        refreshed_settings = Settings(
            systems=[
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        platform="core",
                    ),
                )
            ],
            default_system_id="archive-core",
        )
        setup_service = MagicMock()
        setup_service.delete_system.return_value = ("QS CryoStorage", "archive-core")
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}
        history_store = MagicMock()
        history_store.delete_system_history.return_value = {
            "tracked_slots": 1,
            "event_count": 2,
            "metric_sample_count": 3,
            "total_rows": 6,
            "removed_system_ids": ["qs-cryostorage"],
        }

        with patch("admin_service.main.reload_app_settings", side_effect=[initial_settings, refreshed_settings]):
            with patch("admin_service.main.SystemSetupService", return_value=setup_service):
                with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                    with patch("admin_service.main.get_history_store", return_value=history_store):
                        response = asyncio.run(route.endpoint(system_id="qs-cryostorage", purge_history=True))

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["history_purge"]["requested"])
        self.assertTrue(payload["history_purge"]["ok"])
        self.assertEqual(payload["history_purge"]["summary"]["total_rows"], 6)
        history_store.delete_system_history.assert_called_once_with("qs-cryostorage")
        runtime_service.mark_restart_required.assert_called_once_with(("ui",))

    def test_delete_system_route_redacts_history_purge_failure_detail(self) -> None:
        route = next(
            route for route in admin_app.routes
            if route.path == "/api/admin/system-setup/{system_id}" and "DELETE" in getattr(route, "methods", set())
        )
        initial_settings = Settings(
            systems=[
                SystemConfig(
                    id="qs-cryostorage",
                    label="QS CryoStorage",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        platform="quantastor",
                    ),
                )
            ],
            default_system_id="qs-cryostorage",
        )
        refreshed_settings = Settings(systems=[], default_system_id=None)
        setup_service = MagicMock()
        setup_service.delete_system.return_value = ("QS CryoStorage", None)
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}
        history_store = MagicMock()
        history_store.delete_system_history.side_effect = RuntimeError("Traceback: token=history-secret")

        with patch("admin_service.main.reload_app_settings", side_effect=[initial_settings, refreshed_settings, refreshed_settings]):
            with patch("admin_service.main.SystemSetupService", return_value=setup_service):
                with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                    with patch("admin_service.main.get_history_store", return_value=history_store):
                        response = asyncio.run(route.endpoint(system_id="qs-cryostorage", purge_history=True))

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["history_purge"]["ok"])
        self.assertEqual(payload["history_purge"]["detail"], "Saved history purge failed; see admin logs.")
        joined = json.dumps(payload)
        self.assertNotIn("Traceback", joined)
        self.assertNotIn("history-secret", joined)

    def test_purge_orphaned_history_route_returns_cleanup_summary(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/history/purge-orphaned")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        platform="core",
                    ),
                )
            ],
            default_system_id="archive-core",
        )
        history_store = MagicMock()
        history_store.purge_orphaned_history.return_value = {
            "tracked_slots": 1,
            "event_count": 2,
            "metric_sample_count": 5,
            "total_rows": 8,
            "removed_system_ids": ["qs-cryostorage"],
        }

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_history_store", return_value=history_store):
                response = asyncio.run(route.endpoint())

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["removed_system_ids"], ["qs-cryostorage"])
        self.assertEqual(payload["valid_system_ids"], ["archive-core"])
        history_store.purge_orphaned_history.assert_called_once_with(["archive-core"])

    def test_list_orphaned_history_route_returns_history_sources(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/history/orphaned")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        platform="core",
                    ),
                )
            ],
            default_system_id="archive-core",
        )
        history_store = MagicMock()
        history_store.list_history_system_summaries.return_value = [
            {
                "system_id": "qs-cryostorage",
                "system_label": "QS CryoStorage",
                "tracked_slots": 1,
                "event_count": 2,
                "metric_sample_count": 5,
                "total_rows": 8,
            }
        ]

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_history_store", return_value=history_store):
                response = asyncio.run(route.endpoint())

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["orphaned_systems"][0]["system_id"], "qs-cryostorage")
        self.assertEqual(payload["valid_system_ids"], ["archive-core"])
        history_store.list_history_system_summaries.assert_called_once_with(["archive-core"])

    def test_adopt_removed_system_history_route_redacts_inspection_failure_detail(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/history/adopt-removed-system")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="example-qs-ha",
                    label="ExampleQS HA",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        platform="quantastor",
                    ),
                )
            ],
            default_system_id="example-qs-ha",
        )
        history_store = MagicMock()
        history_store.list_history_system_summaries.side_effect = RuntimeError(
            "Traceback: token=history-secret"
        )

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_history_store", return_value=history_store):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        route.endpoint(
                            payload=HistoryAdoptRequest(
                                source_system_id="qs-cryostorage",
                                target_system_id="example-qs-ha",
                            )
                        )
                    )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "Unable to inspect orphaned history; see admin logs.")
        self.assertNotIn("Traceback", str(raised.exception.detail))
        self.assertNotIn("history-secret", str(raised.exception.detail))

    def test_adopt_removed_system_history_route_rehomes_orphaned_history(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/history/adopt-removed-system")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="example-qs-ha",
                    label="ExampleQS HA",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        platform="quantastor",
                    ),
                )
            ],
            default_system_id="example-qs-ha",
        )
        history_store = MagicMock()
        history_store.list_history_system_summaries.side_effect = [
            [
                {
                    "system_id": "qs-cryostorage",
                    "system_label": "QS CryoStorage",
                    "tracked_slots": 2,
                    "event_count": 3,
                    "metric_sample_count": 4,
                    "total_rows": 9,
                }
            ],
            [],
        ]
        history_store.adopt_system_history.return_value = {
            "source_system_id": "qs-cryostorage",
            "target_system_id": "example-qs-ha",
            "target_system_label": "ExampleQS HA",
            "tracked_slots": 2,
            "event_count": 3,
            "metric_sample_count": 4,
            "total_rows": 9,
            "slot_state_conflicts": 1,
        }

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_history_store", return_value=history_store):
                response = asyncio.run(
                    route.endpoint(
                        payload=HistoryAdoptRequest(
                            source_system_id="qs-cryostorage",
                            target_system_id="example-qs-ha",
                        )
                    )
                )

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_rows"], 9)
        self.assertEqual(payload["target_system_id"], "example-qs-ha")
        self.assertEqual(payload["orphaned_systems"], [])
        history_store.adopt_system_history.assert_called_once_with(
            "qs-cryostorage",
            "example-qs-ha",
            target_system_label="ExampleQS HA",
        )

    def test_adopt_removed_system_history_route_redacts_adoption_failure_detail(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/history/adopt-removed-system")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="example-qs-ha",
                    label="ExampleQS HA",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        platform="quantastor",
                    ),
                )
            ],
            default_system_id="example-qs-ha",
        )
        history_store = MagicMock()
        history_store.list_history_system_summaries.return_value = [
            {
                "system_id": "qs-cryostorage",
                "system_label": "QS CryoStorage",
                "tracked_slots": 2,
                "event_count": 3,
                "metric_sample_count": 4,
                "total_rows": 9,
            }
        ]
        history_store.adopt_system_history.side_effect = RuntimeError("Traceback: token=history-secret")

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_history_store", return_value=history_store):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        route.endpoint(
                            payload=HistoryAdoptRequest(
                                source_system_id="qs-cryostorage",
                                target_system_id="example-qs-ha",
                            )
                        )
                    )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "Unable to adopt removed system history; see admin logs.")
        self.assertNotIn("Traceback", str(raised.exception.detail))
        self.assertNotIn("history-secret", str(raised.exception.detail))

    def test_storage_view_candidate_route_returns_unmapped_inventory_candidates(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/storage-views/candidates")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        api_key="token",
                        platform="core",
                    ),
                )
            ],
            default_system_id="archive-core",
        )
        service = MagicMock()
        service.system.id = "archive-core"
        service.get_storage_view_candidates = AsyncMock(
            return_value=[
                {
                    "candidate_id": "SER-NVME-1",
                    "label": "SER-NVME-1",
                    "serial": "SER-NVME-1",
                    "device_names": ["nvd0"],
                    "recommended_binding": {
                        "serials": ["SER-NVME-1"],
                        "pcie_addresses": ["0000:5e:00.0"],
                        "device_names": ["nvd0"],
                    },
                }
            ]
        )
        registry = MagicMock()
        registry.get_service.return_value = service

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.InventoryRegistry", return_value=registry):
                response = asyncio.run(route.endpoint(system_id="archive-core", force=True))

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["system_id"], "archive-core")
        self.assertEqual(payload["candidates"][0]["serial"], "SER-NVME-1")
        service.get_storage_view_candidates.assert_awaited_once_with(force_refresh=True, target_system_id=None)

    def test_quantastor_node_discovery_route_returns_hardware_backed_nodes(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/quantastor-nodes")
        client = AsyncMock()
        client.fetch_all.return_value = TrueNASRawData(
            enclosures=[],
            systems=[
                {"id": "cluster", "name": "Cluster View", "storageSystemClusterId": "cluster-a"},
                {"id": "node-a", "name": "ExampleQS Left", "mainIpAddress": "192.0.2.30", "storageSystemClusterId": "cluster-a"},
                {"id": "node-b", "name": "ExampleQS Right", "managementIpAddress": "192.0.2.31", "storageSystemClusterId": "cluster-a", "isMaster": True},
                {"id": "qs-cryostorage", "name": "QS CryoStorage", "hostname": "10.88.88.30", "storageSystemClusterId": "cluster-a"},
            ],
            disks=[],
            pools=[],
            pool_devices=[],
            ha_groups=[],
            hw_disks=[],
            hw_enclosures=[
                {"id": "enc-a", "storageSystemId": "node-a"},
                {"id": "enc-b", "storageSystemId": "node-b"},
            ],
            disk_temperatures={},
            smart_test_results=[],
        )

        with patch("admin_service.main.QuantastorRESTClient", return_value=client):
            response = asyncio.run(
                route.endpoint(
                    QuantastorNodeDiscoveryRequest(
                        truenas_host="https://10.13.37.40",
                        api_user="jbodmap",
                        api_password="secret",
                        verify_ssl=False,
                    )
                )
            )

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual([node["system_id"] for node in payload["nodes"]], ["node-a", "node-b"])
        self.assertEqual(payload["nodes"][1]["host"], "192.0.2.31")

    def test_quantastor_node_discovery_route_fills_hosts_from_ssh_gateway_ports(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/quantastor-nodes")
        settings = Settings(ssh=SSHConfig(known_hosts_path="/runtime/data/known_hosts"))
        client = AsyncMock()
        client.fetch_all.return_value = TrueNASRawData(
            enclosures=[],
            systems=[
                {"id": "node-a", "name": "ExampleQS Left", "storageSystemClusterId": "cluster-a"},
                {"id": "node-b", "name": "ExampleQS Right", "storageSystemClusterId": "cluster-a"},
            ],
            disks=[],
            pools=[],
            pool_devices=[],
            ha_groups=[],
            hw_disks=[],
            hw_enclosures=[
                {"id": "enc-a", "storageSystemId": "node-a"},
                {"id": "enc-b", "storageSystemId": "node-b"},
            ],
            disk_temperatures={},
            smart_test_results=[],
        )
        network_ports = json.dumps(
            [
                {
                    "name": "eno1",
                    "storageSystemId": "node-a",
                    "ipAddress": "192.0.2.30",
                    "gateway": "192.0.2.1",
                },
                {
                    "name": "eno1",
                    "storageSystemId": "node-b",
                    "ipAddress": "192.0.2.31",
                    "gateway": "192.0.2.1",
                },
            ]
        )
        probe = MagicMock()
        probe.run_commands = AsyncMock(
            return_value=[
                SSHCommandResult(
                    command="/usr/bin/qs network-port-list --json",
                    ok=True,
                    stdout=network_ports,
                    exit_code=0,
                )
            ]
        )

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.QuantastorRESTClient", return_value=client):
                with patch("admin_service.main.SSHProbe", return_value=probe) as ssh_probe:
                    response = asyncio.run(
                        route.endpoint(
                            QuantastorNodeDiscoveryRequest(
                                truenas_host="https://quantastor.example.test",
                                api_user="jbodmap",
                                api_password="secret",
                                verify_ssl=False,
                                ssh_enabled=True,
                                ssh_host="quantastor.example.test",
                                ssh_user="jbodmap",
                                ssh_key_path="/run/ssh/id_jbodmap",
                                ssh_known_hosts_path="/request-selected-known-hosts",
                                ssh_strict_host_key_checking=False,
                                ha_nodes=[
                                    {"system_id": "node-a", "label": "ExampleQS Left", "host": "192.0.2.30"},
                                    {"system_id": "node-b", "label": "ExampleQS Right"},
                                ],
                            )
                        )
                    )

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([node["host"] for node in payload["nodes"]], ["192.0.2.30", "192.0.2.31"])
        self.assertTrue(payload["host_discovery"]["ok"])
        self.assertEqual(payload["host_discovery"]["filled_hosts"], 1)
        probe.run_commands.assert_awaited_once()
        self.assertEqual(ssh_probe.call_args.args[0].known_hosts_path, "/runtime/data/known_hosts")

    def test_live_enclosures_route_returns_resolved_profile_info(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/storage-views/live-enclosures")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(
                        host="https://archive-core.local",
                        api_key="token",
                        platform="core",
                    ),
                )
            ],
            default_system_id="archive-core",
        )
        service = MagicMock()
        service.system = settings.systems[0]
        service.system.id = "archive-core"
        service.settings = settings
        service.get_snapshot = AsyncMock(
            return_value=MagicMock(
                enclosures=[
                    EnclosureOption(
                        id="50030480090c4f7f",
                        label="Front 24 Bay",
                        name="SES Front 24",
                        rows=6,
                        columns=4,
                        slot_count=24,
                        slot_layout=[[0, 6, 12, 18]],
                    )
                ]
            )
        )
        service.profile_registry.resolve_for_enclosure.return_value = MagicMock(
            id="supermicro-ssg-6048r-front-24",
            label="Supermicro SSG-6048R Front 24",
        )
        registry = MagicMock()
        registry.get_service.return_value = service

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.InventoryRegistry", return_value=registry):
                response = asyncio.run(route.endpoint(system_id="archive-core", force=False))

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["system_id"], "archive-core")
        self.assertEqual(payload["enclosures"][0]["id"], "50030480090c4f7f")
        self.assertEqual(payload["enclosures"][0]["label"], "Front 24 Bay")
        self.assertEqual(payload["enclosures"][0]["profile_id"], "supermicro-ssg-6048r-front-24")
        self.assertEqual(payload["enclosures"][0]["profile_label"], "Supermicro SSG-6048R Front 24")
        service.get_snapshot.assert_awaited_once_with(force_refresh=False)

    def test_save_profile_route_returns_updated_profile_list(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/profiles" and "POST" in getattr(route, "methods", set()))
        initial_settings = Settings(
            config_file="C:/tmp/config/config.yaml",
            paths=PathConfig(
                mapping_file="C:/tmp/data/slot_mappings.json",
                log_file="C:/tmp/logs/app.log",
                profile_file="C:/tmp/config/profiles.yaml",
                slot_detail_cache_file="C:/tmp/data/slot_detail_cache.json",
            ),
        )
        refreshed_settings = Settings(
            config_file="C:/tmp/config/config.yaml",
            paths=PathConfig(
                mapping_file="C:/tmp/data/slot_mappings.json",
                log_file="C:/tmp/logs/app.log",
                profile_file="C:/tmp/config/profiles.yaml",
                slot_detail_cache_file="C:/tmp/data/slot_detail_cache.json",
            ),
            profiles=[
                EnclosureProfileConfig(
                    id="custom-front-24",
                    label="Custom Front 24",
                    summary="Saved custom front-drive profile.",
                    face_style="front-drive",
                    latch_edge="top",
                    bay_size="2.5",
                    rows=1,
                    columns=24,
                    slot_layout=[list(range(24))],
                )
            ],
        )
        profile_service = MagicMock()
        profile_service.save_profile.return_value = (refreshed_settings.profiles[0], False)
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}

        with patch("admin_service.main.reload_app_settings", side_effect=[initial_settings, refreshed_settings]):
            with patch("admin_service.main.ProfileBuilderService", return_value=profile_service):
                with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                    response = asyncio.run(
                        route.endpoint(
                            payload=EnclosureProfileRequest(
                                source_profile_id="generic-front-24-1x24",
                                id="custom-front-24",
                                label="Custom Front 24",
                                summary="Saved custom front-drive profile.",
                                face_style="front-drive",
                                latch_edge="top",
                                bay_size="2.5",
                                rows=1,
                                columns=24,
                                slot_count=24,
                            )
                        )
                    )

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"]["id"], "custom-front-24")
        self.assertFalse(payload["updated_existing"])
        self.assertIn("custom-front-24", [profile["id"] for profile in payload["profiles"]])
        runtime_service.mark_restart_required.assert_called_once_with(("ui",))

    def test_delete_profile_route_returns_updated_profile_list(self) -> None:
        route = next(
            route
            for route in admin_app.routes
            if route.path == "/api/admin/profiles/{profile_id}" and "DELETE" in getattr(route, "methods", set())
        )
        initial_settings = Settings(
            config_file="C:/tmp/config/config.yaml",
            paths=PathConfig(
                mapping_file="C:/tmp/data/slot_mappings.json",
                log_file="C:/tmp/logs/app.log",
                profile_file="C:/tmp/config/profiles.yaml",
                slot_detail_cache_file="C:/tmp/data/slot_detail_cache.json",
            ),
            profiles=[
                EnclosureProfileConfig(
                    id="custom-front-24",
                    label="Custom Front 24",
                    summary="Saved custom front-drive profile.",
                    face_style="front-drive",
                    latch_edge="top",
                    bay_size="2.5",
                    rows=1,
                    columns=24,
                    slot_layout=[list(range(24))],
                )
            ],
        )
        refreshed_settings = Settings(
            config_file="C:/tmp/config/config.yaml",
            paths=PathConfig(
                mapping_file="C:/tmp/data/slot_mappings.json",
                log_file="C:/tmp/logs/app.log",
                profile_file="C:/tmp/config/profiles.yaml",
                slot_detail_cache_file="C:/tmp/data/slot_detail_cache.json",
            ),
        )
        profile_service = MagicMock()
        profile_service.delete_profile.return_value = "Custom Front 24"
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {"available": True, "detail": None, "containers": []}

        with patch("admin_service.main.reload_app_settings", side_effect=[initial_settings, refreshed_settings]):
            with patch("admin_service.main.ProfileBuilderService", return_value=profile_service):
                with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                    response = asyncio.run(route.endpoint(profile_id="custom-front-24"))

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deleted_label"], "Custom Front 24")
        self.assertNotIn("custom-front-24", [profile["id"] for profile in payload["profiles"]])
        runtime_service.mark_restart_required.assert_called_once_with(("ui",))

    def test_sudoers_preview_route_returns_exact_rendered_content(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/sudoers-preview")

        response = asyncio.run(
            route.endpoint(
                SystemSetupSudoPreviewRequest(
                    platform="scale",
                    service_user="jbodmap",
                    install_sudo_rules=True,
                    sudo_commands=[
                        "/usr/sbin/zpool status -gP",
                        "sudo -n /usr/bin/sg_ses -p aes /dev/sg26",
                        "sudo -n /usr/bin/sg_ses -p ec /dev/sg37",
                        "sudo -n /usr/bin/sg_ses --join --filter /dev/sg26",
                    ],
                )
            )
        )
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["filename"], "truenas-jbod-ui-jbodmap")
        self.assertIn("/usr/local/etc/sudoers.d/truenas-jbod-ui-jbodmap", payload["path_candidates"])
        self.assertIn("Cmnd_Alias JBODMAP_SCALE_CMDS", payload["content"])
        self.assertIn("/usr/bin/sg_ses -p aes /dev/sg*", payload["content"])
        self.assertIn("/usr/bin/sg_ses -p ec /dev/sg*", payload["content"])
        self.assertIn("/usr/bin/sg_ses --join --filter /dev/sg*", payload["content"])
        self.assertNotIn("/usr/sbin/zpool status -gP", payload["content"])

    def test_sudoers_preview_route_includes_core_mprutil_topology_rules(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/sudoers-preview")

        response = asyncio.run(
            route.endpoint(
                SystemSetupSudoPreviewRequest(
                    platform="core",
                    service_user="jbodmap",
                    install_sudo_rules=True,
                    sudo_commands=[
                        "sudo -n /usr/sbin/sesutil show",
                        "sudo -n /usr/sbin/mprutil -u 1 show expanders",
                    ],
                )
            )
        )
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["filename"], "midclt user.update USER_ID")
        self.assertEqual(payload["path_candidates"], [])
        self.assertIn("TrueNAS CORE bootstrap runs this same one-line middleware command", payload["detail"])
        self.assertIn("midclt call user.update USER_ID", payload["content"])
        self.assertNotIn("Cmnd_Alias", payload["content"])
        command_tokens = shlex.split(payload["content"].strip())
        command_payload = json.loads(command_tokens[-1])
        self.assertIn("/usr/sbin/mprutil show adapters", command_payload["sudo_commands"])
        self.assertIn("/usr/sbin/mprutil -u * show expanders", command_payload["sudo_commands"])
        self.assertIn("/usr/local/sbin/dmidecode -t slot", command_payload["sudo_commands"])
        self.assertIn("/usr/bin/tail -n 4000 /var/log/messages", command_payload["sudo_commands"])
        self.assertNotIn("/usr/sbin/mprutil -u 1 show expanders", command_payload["sudo_commands"])
        self.assertNotIn("/usr/sbin/mprutil *", command_payload["sudo_commands"])

    def test_sudoers_preview_route_handles_disabled_rules(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/sudoers-preview")

        response = asyncio.run(
            route.endpoint(
                SystemSetupSudoPreviewRequest(
                    platform="core",
                    service_user="readonly",
                    install_sudo_rules=False,
                    sudo_commands=["sudo -n /usr/sbin/sesutil show"],
                )
            )
        )
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["filename"], "midclt user.update USER_ID")
        self.assertIn("skip running", payload["detail"])
        self.assertIn("# CORE midclt permission update disabled", payload["content"])

    def test_sudoers_preview_route_disables_esxi_bootstrap_flow(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/sudoers-preview")

        response = asyncio.run(
            route.endpoint(
                SystemSetupSudoPreviewRequest(
                    platform="esxi",
                    service_user="root",
                    install_sudo_rules=True,
                    sudo_commands=[],
                )
            )
        )
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["filename"], "truenas-jbod-ui-root")
        self.assertIn("does not use the Linux one-time bootstrap or sudoers flow", payload["detail"])
        self.assertIn("# VMware ESXi does not use the Linux sudoers/bootstrap flow.", payload["content"])

    def test_bootstrap_route_rejects_esxi_platform(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/bootstrap")
        settings = Settings(config_file="C:/tmp/config/config.yaml")

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with self.assertRaises(HTTPException) as context:
                asyncio.run(
                    route.endpoint(
                        SystemSetupBootstrapRequest(
                            platform="esxi",
                            host="10.88.88.20",
                            bootstrap_user="root",
                            bootstrap_password="secret",
                            service_user="root",
                            service_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKeyOnly esxi-test",
                            install_sudo_rules=False,
                        )
                    )
                )

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("does not use the Linux one-time bootstrap or sudoers flow", str(context.exception.detail))

    def test_esxi_host_prep_upload_route_stages_raw_body_and_returns_packages(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/esxi-host-prep/upload")
        request, receive_probe = make_streaming_request(
            [b"storcli-", b"bytes"],
            content_length=len(b"storcli-bytes"),
        )
        request.body = AsyncMock(side_effect=AssertionError("request.body() must not be used"))
        host_prep_service = MagicMock()
        host_prep_service.stage_package.return_value = {
            "token": "storcli-1",
            "filename": "BCM-vmware-storcli64.zip",
        }
        host_prep_service.list_staged_packages.return_value = [host_prep_service.stage_package.return_value]

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "host-prep-upload"
            workspace.mkdir()
            with patch("admin_service.main.tempfile.mkdtemp", return_value=str(workspace)):
                with patch("admin_service.main.get_esxi_host_prep_service", return_value=host_prep_service):
                    response = asyncio.run(route.endpoint(request=request, filename="BCM-vmware-storcli64.zip"))
            self.assertFalse(workspace.exists())

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["package"]["token"], "storcli-1")
        self.assertEqual(payload["packages"][0]["filename"], "BCM-vmware-storcli64.zip")
        self.assertEqual(receive_probe.call_count, 2)
        request.body.assert_not_called()
        host_prep_service.stage_package.assert_called_once_with("BCM-vmware-storcli64.zip", b"storcli-bytes")

    def test_esxi_host_prep_upload_rejects_declared_oversize_without_reading_stream(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/esxi-host-prep/upload")
        request, receive_probe = make_streaming_request(
            [b"ignored"],
            content_length=MAX_UPLOAD_BYTES + 1,
        )
        host_prep_service = MagicMock()
        host_prep_service.stage_package.return_value = {
            "token": "should-not-stage",
            "filename": "BCM-vmware-storcli64.zip",
        }
        host_prep_service.list_staged_packages.return_value = []

        with patch("admin_service.main.tempfile.mkdtemp") as make_workspace:
            with patch("admin_service.main.get_esxi_host_prep_service", return_value=host_prep_service) as get_service:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(route.endpoint(request=request, filename="BCM-vmware-storcli64.zip"))

        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(
            raised.exception.detail,
            "ESXi host-prep upload request body is too large.",
        )
        receive_probe.assert_not_called()
        make_workspace.assert_not_called()
        get_service.assert_not_called()
        host_prep_service.stage_package.assert_not_called()

    def test_esxi_host_prep_install_route_returns_install_status_payload(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/esxi-host-prep/install")
        settings = Settings(ssh=SSHConfig(known_hosts_path="/runtime/data/known_hosts"))
        host_prep_service = MagicMock()
        host_prep_service.install_package.return_value = {
            "ok": False,
            "detail": "StorCLI is installed, but no compatible MegaRAID controller is currently visible to it on this ESXi host.",
            "remote_path": "/tmp/truenas-jbod-ui-storcli.zip",
            "install_command": "esxcli software component apply -d /tmp/truenas-jbod-ui-storcli.zip",
            "install_result": {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Controller 0 not found"},
            "verification": {"summary": {"controller_visible": False, "controller_count": 0}},
            "cleanup_result": {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""},
        }
        host_prep_service.list_staged_packages.return_value = [
            {"token": "storcli-1", "filename": "BCM-vmware-storcli64.zip"}
        ]

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_esxi_host_prep_service", return_value=host_prep_service):
                response = asyncio.run(
                    route.endpoint(
                        payload=ESXiHostPrepInstallRequest(
                            host="192.0.2.121",
                            user="root",
                            password="secret",
                            known_hosts_path="/request-selected-known-hosts",
                            strict_host_key_checking=False,
                            upload_token="storcli-1",
                        )
                    )
                )

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["install_ok"])
        self.assertIn("no compatible MegaRAID controller", payload["detail"])
        self.assertEqual(payload["packages"][0]["token"], "storcli-1")
        self.assertEqual(
            host_prep_service.install_package.call_args.kwargs["known_hosts_path"],
            "/runtime/data/known_hosts",
        )

    def test_system_setup_request_preserves_distinct_quantastor_label_only_nodes(self) -> None:
        payload = SystemSetupRequest(
            label="Quantastor HA",
            platform="quantastor",
            truenas_host="https://192.0.2.30",
            ha_enabled=True,
            ha_nodes=[
                {"label": "Node Alpha"},
                {"label": "Node Beta"},
            ],
        )

        self.assertEqual(
            [node.label for node in payload.ha_nodes],
            ["Node Alpha", "Node Beta"],
        )

    def test_esxi_host_prep_route_resolves_saved_password_sentinel(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/esxi-host-prep/install")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-esxi",
                    label="Saved ESXi",
                    truenas=TrueNASConfig(host="192.0.2.25", platform="esxi"),
                    ssh=SSHConfig(
                        enabled=True,
                        host="192.0.2.25",
                        user="root",
                        password=MARKER_ALPHA,
                        known_hosts_path="/app/data/known_hosts",
                        timeout_seconds=240,
                    ),
                )
            ]
        )
        host_prep_service = MagicMock()
        host_prep_service.install_package.return_value = {"ok": True, "detail": "installed"}
        host_prep_service.list_staged_packages.return_value = []

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_esxi_host_prep_service", return_value=host_prep_service):
                asyncio.run(
                    route.endpoint(
                        payload=ESXiHostPrepInstallRequest(
                            system_id="saved-esxi",
                            host="192.0.2.25",
                            user="root",
                            password=PRESERVE_SECRET_SENTINEL,
                            timeout_seconds=240,
                            upload_token="storcli-1",
                        )
                    )
                )

        install_payload = host_prep_service.install_package.call_args.args[0]
        self.assertEqual(install_payload.password, MARKER_ALPHA)
        self.assertEqual(install_payload.timeout_seconds, 240)

    def test_esxi_host_prep_rejects_saved_password_for_different_endpoint(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/esxi-host-prep/install")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-esxi",
                    label="Saved ESXi",
                    truenas=TrueNASConfig(host="192.0.2.25", platform="esxi"),
                    ssh=SSHConfig(
                        enabled=True,
                        host="192.0.2.25",
                        user="root",
                        password=MARKER_ALPHA,
                    ),
                )
            ]
        )
        host_prep_service = MagicMock()

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_esxi_host_prep_service", return_value=host_prep_service):
                with self.assertRaises(HTTPException) as captured:
                    asyncio.run(
                        route.endpoint(
                            payload=ESXiHostPrepInstallRequest(
                                system_id="saved-esxi",
                                host="198.51.100.25",
                                user="root",
                                password=PRESERVE_SECRET_SENTINEL,
                                upload_token="storcli-1",
                            )
                        )
                    )

        self.assertEqual(captured.exception.status_code, 400)
        host_prep_service.install_package.assert_not_called()

    def test_quantastor_discovery_route_resolves_saved_secret_sentinels(self) -> None:
        route = next(
            route
            for route in admin_app.routes
            if route.path == "/api/admin/system-setup/quantastor-nodes"
        )
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-quantastor",
                    label="Saved Quantastor",
                    truenas=TrueNASConfig(
                        host="https://192.0.2.30",
                        platform="quantastor",
                        api_user="admin",
                        api_password=MARKER_ALPHA,
                    ),
                    ssh=SSHConfig(
                        enabled=True,
                        host="192.0.2.31",
                        user="svc",
                        password=MARKER_BRAVO,
                        known_hosts_path="/app/data/known_hosts",
                        timeout_seconds=45,
                    ),
                )
            ]
        )
        client = MagicMock()
        client.fetch_all = AsyncMock(return_value=SimpleNamespace())
        enrich = AsyncMock(return_value={"attempted": False, "ok": True})

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.QuantastorRESTClient", return_value=client) as client_factory:
                with patch("admin_service.main.serialize_quantastor_nodes", return_value=[]):
                    with patch("admin_service.main.enrich_quantastor_nodes_from_ssh", enrich):
                        asyncio.run(
                            route.endpoint(
                                QuantastorNodeDiscoveryRequest(
                                    system_id="saved-quantastor",
                                    truenas_host="https://192.0.2.30",
                                    api_user="admin",
                                    api_password=PRESERVE_SECRET_SENTINEL,
                                    ssh_enabled=True,
                                    ssh_host="192.0.2.31",
                                    ssh_user="svc",
                                    ssh_password=PRESERVE_SECRET_SENTINEL,
                                    ssh_timeout_seconds=45,
                                )
                            )
                        )

        api_config = client_factory.call_args.args[0]
        discovery_payload = enrich.call_args.args[0]
        self.assertEqual(api_config.api_password, MARKER_ALPHA)
        self.assertEqual(discovery_payload.ssh_password, MARKER_BRAVO)
        self.assertEqual(discovery_payload.ssh_timeout_seconds, 45)

    def test_quantastor_discovery_rejects_saved_secrets_for_different_endpoint(self) -> None:
        route = next(
            route
            for route in admin_app.routes
            if route.path == "/api/admin/system-setup/quantastor-nodes"
        )
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-quantastor",
                    label="Saved Quantastor",
                    truenas=TrueNASConfig(
                        host="https://192.0.2.30",
                        platform="quantastor",
                        api_user="admin",
                        api_password=MARKER_ALPHA,
                    ),
                    ssh=SSHConfig(
                        enabled=True,
                        host="192.0.2.31",
                        user="svc",
                        password=MARKER_BRAVO,
                    ),
                )
            ]
        )

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.QuantastorRESTClient") as client_factory:
                with self.assertRaises(HTTPException) as captured:
                    asyncio.run(
                        route.endpoint(
                            QuantastorNodeDiscoveryRequest(
                                system_id="saved-quantastor",
                                truenas_host="https://198.51.100.30",
                                api_user="admin",
                                api_password=PRESERVE_SECRET_SENTINEL,
                            )
                        )
                    )

        self.assertEqual(captured.exception.status_code, 400)
        client_factory.assert_not_called()

    def test_quantastor_discovery_rejects_saved_secrets_for_case_distinct_api_path(self) -> None:
        route = next(
            route
            for route in admin_app.routes
            if route.path == "/api/admin/system-setup/quantastor-nodes"
        )
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-quantastor",
                    label="Saved Quantastor",
                    truenas=TrueNASConfig(
                        host="https://192.0.2.30/Tenant",
                        platform="quantastor",
                        api_user="admin",
                        api_password=MARKER_ALPHA,
                    ),
                )
            ]
        )

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.QuantastorRESTClient") as client_factory:
                with self.assertRaises(HTTPException) as captured:
                    asyncio.run(
                        route.endpoint(
                            QuantastorNodeDiscoveryRequest(
                                system_id="saved-quantastor",
                                truenas_host="https://192.0.2.30/tenant",
                                api_user="admin",
                                api_password=PRESERVE_SECRET_SENTINEL,
                            )
                        )
                    )

        self.assertEqual(captured.exception.status_code, 400)
        client_factory.assert_not_called()

    def test_quantastor_discovery_rejects_saved_secrets_for_identical_invalid_port(self) -> None:
        route = next(
            route
            for route in admin_app.routes
            if route.path == "/api/admin/system-setup/quantastor-nodes"
        )
        invalid_endpoint = "https://192.0.2.30:not-a-port/Tenant"
        settings = Settings(
            systems=[
                SystemConfig(
                    id="saved-quantastor",
                    label="Saved Quantastor",
                    truenas=TrueNASConfig(
                        host=invalid_endpoint,
                        platform="quantastor",
                        api_user="admin",
                        api_password=MARKER_ALPHA,
                    ),
                )
            ]
        )

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.QuantastorRESTClient") as client_factory:
                with self.assertRaises(HTTPException) as captured:
                    asyncio.run(
                        route.endpoint(
                            QuantastorNodeDiscoveryRequest(
                                system_id="saved-quantastor",
                                truenas_host=invalid_endpoint,
                                api_user="admin",
                                api_password=PRESERVE_SECRET_SENTINEL,
                            )
                        )
                    )

        self.assertEqual(captured.exception.status_code, 400)
        client_factory.assert_not_called()


class AdminBootstrapEmbeddingTests(unittest.TestCase):
    def test_admin_bootstrap_payload_is_script_safe(self) -> None:
        payload = {
            "systems": [
                {
                    "id": "lab",
                    "label": "</script><script>alert(1)</script>",
                    "note": "line one" + chr(0x2028) + "line two <!-- not a comment",
                }
            ]
        }

        stub_request = SimpleNamespace(
            url_for=lambda name, **path_params: f"/static/{path_params.get('path', '')}"
        )
        rendered = admin_templates.get_template("index.html").render(
            request=stub_request,
            admin_bootstrap_json=json.dumps(payload),
        )

        self.assertIn("window.ADMIN_BOOTSTRAP = {", rendered)
        self.assertNotIn("</script><script>alert(1)", rendered)
        self.assertNotIn(chr(0x2028), rendered)
        self.assertNotIn("<!--", rendered)

        start = rendered.index("window.ADMIN_BOOTSTRAP = ") + len("window.ADMIN_BOOTSTRAP = ")
        end = rendered.index(";\n", start)
        self.assertEqual(json.loads(rendered[start:end]), payload)
