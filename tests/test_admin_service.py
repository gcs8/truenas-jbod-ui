from __future__ import annotations

import asyncio
import json
import urllib.error
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi import Request

from admin_service.config import AdminSettings
from admin_service.main import app as admin_app
from admin_service.main import build_admin_state_payload
from admin_service.main import decode_optional_secret_header
from app.config import (
    AdminSurfaceConfig,
    EnclosureProfileConfig,
    PathConfig,
    SSHConfig,
    Settings,
    SystemConfig,
    TrueNASConfig,
)
from app.main import app as main_app
from app.main import resolve_admin_launch_url
from app.models.domain import EnclosureOption
from app.models.domain import EnclosureProfileRequest
from app.models.domain import HistoryAdoptRequest
from app.models.domain import QuantastorNodeDiscoveryRequest
from app.models.domain import SystemSetupBootstrapRequest
from app.models.domain import SystemSetupSudoPreviewRequest
from app.services.profile_registry import UNIFI_UNVR_FRONT_4_PROFILE_ID
from history_service.config import HistorySettings
from history_service.main import app as history_app
from app.services.truenas_ws import TrueNASRawData


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


class MainAppBoundaryTests(unittest.TestCase):
    @staticmethod
    def _call_main_route(path: str) -> object:
        route = next(route for route in main_app.routes if route.path == path)
        return asyncio.run(route.endpoint())

    def test_admin_sidecar_exposes_one_time_bootstrap_route(self) -> None:
        paths = {route.path for route in admin_app.routes}

        self.assertIn("/api/admin/system-setup/bootstrap", paths)
        self.assertIn("/api/admin/system-setup/sudoers-preview", paths)
        self.assertIn("/api/admin/system-setup/{system_id}", paths)
        self.assertIn("/api/admin/system-setup/demo", paths)
        self.assertIn("/api/admin/profiles", paths)
        self.assertIn("/api/admin/profiles/{profile_id}", paths)
        self.assertIn("/api/admin/history/purge-orphaned", paths)
        self.assertIn("/api/admin/history/orphaned", paths)
        self.assertIn("/api/admin/history/adopt-removed-system", paths)
        self.assertIn("/api/admin/debug/export", paths)

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

    def test_main_app_exposes_storage_view_runtime_route(self) -> None:
        paths = {route.path for route in main_app.routes}

        self.assertIn("/api/storage-views", paths)
        self.assertIn("/api/storage-views/{view_id}/slots/{slot_index}/history", paths)
        self.assertIn("/livez", paths)
        self.assertIn("/healthz", paths)

    def test_main_ui_template_omits_storage_view_runtime_panel(self) -> None:
        template_path = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"
        template_text = template_path.read_text(encoding="utf-8")

        self.assertNotIn('id="storage-views-panel"', template_text)
        self.assertNotIn("Selected Storage View", template_text)

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

    def test_admin_script_disables_linux_bootstrap_flow_for_esxi(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "admin_service" / "static" / "admin.js"
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn("platformSupportsBootstrap", script_text)
        self.assertIn('setupSshSudoPasswordField.classList.toggle("hidden", !savedSudoSupported)', script_text)
        self.assertIn("VMware ESXi does not use the one-time Linux service-account bootstrap.", script_text)
        self.assertIn("VMware ESXi does not use the Linux sudoers/bootstrap path.", script_text)

    def test_main_app_livez_is_lightweight(self) -> None:
        response = self._call_main_route("/livez")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["status"], "ok")

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

        with patch("admin_service.main.reload_app_settings", return_value=settings):
            with patch("admin_service.main.get_runtime_service", return_value=runtime_service):
                with patch("admin_service.main.SSHKeyManager", return_value=key_manager):
                    with patch(
                        "admin_service.main.get_admin_settings",
                        return_value=AdminSettings(auto_stop_seconds=3600),
                    ):
                        with patch(
                            "admin_service.main.get_history_settings",
                            return_value=HistorySettings(sqlite_path="/tmp/history/history.db"),
                        ):
                            payload = asyncio.run(build_admin_state_payload(request))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["admin"]["public_origin"], "http://localhost:8082")
        self.assertEqual(payload["default_system_id"], "archive-core")
        self.assertEqual(payload["systems"][0]["truenas_host"], "https://archive-core.local")
        self.assertFalse(payload["systems"][0]["verify_ssl"])
        self.assertEqual(payload["systems"][0]["tls_ca_bundle_path"], "/app/config/tls/archive-core.pem")
        self.assertEqual(payload["systems"][0]["tls_server_name"], "TrueNAS.gcs8.io")
        self.assertTrue(payload["systems"][0]["ssh_enabled"])
        self.assertEqual(payload["systems"][0]["ssh_key_path"], "/run/ssh/id_truenas")
        self.assertEqual(payload["systems"][0]["storage_views"][0]["id"], "front-bays")
        self.assertEqual(payload["systems"][0]["storage_views"][0]["template_id"], "ses-auto")
        self.assertEqual(payload["systems"][0]["storage_views"][0]["profile_id"], "lab-4x4")
        self.assertEqual(payload["storage_view_templates"][0]["id"], "ses-auto")
        self.assertTrue(any(template["id"] == "aoc-slg4-2h8m2-2" for template in payload["storage_view_templates"]))
        custom_profile = next(profile for profile in payload["profiles"] if profile["id"] == "lab-4x4")
        self.assertEqual(custom_profile["slot_count"], 16)
        self.assertTrue(custom_profile["is_custom"])
        self.assertEqual(custom_profile["reference_count"], 2)
        self.assertIn("core", payload["setup_platform_defaults"])
        self.assertIn("esxi", payload["setup_platform_defaults"])
        self.assertEqual(payload["ssh_keys"][0]["name"], "id_truenas")
        self.assertEqual(payload["paths"]["history_db"], "/tmp/history/history.db")
        self.assertEqual(payload["paths"]["tls_dir"], str(Path("C:/tmp/config") / "tls"))
        self.assertIn("included_paths", payload["backup_defaults"])
        self.assertIn("debug_included_paths", payload["backup_defaults"])
        self.assertTrue(payload["backup_defaults"]["debug_scrub_secrets"])
        self.assertTrue(payload["backup_defaults"]["debug_scrub_disk_identifiers"])
        self.assertTrue(any(group["key"] == "ssh_keys" for group in payload["backup_defaults"]["path_groups"]))

    def test_build_admin_state_payload_includes_quantastor_ha_nodes(self) -> None:
        settings = Settings(
            systems=[
                SystemConfig(
                    id="qsosn-ha",
                    label="QSOSN HA",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        api_user="jbodmap",
                        api_password="secret",
                        platform="quantastor",
                        verify_ssl=False,
                    ),
                    ssh=SSHConfig(
                        enabled=True,
                        host="10.13.37.30",
                        extra_hosts=["10.13.37.31"],
                        ha_enabled=True,
                        ha_nodes=[
                            {
                                "system_id": "node-a",
                                "label": "QSOSN Left",
                                "host": "10.13.37.30",
                            },
                            {
                                "system_id": "node-b",
                                "label": "QSOSN Right",
                                "host": "10.13.37.31",
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
                                "pool_names": ["QSOSN-BOOT-B"],
                                "serials": [],
                                "pcie_addresses": [],
                                "device_names": ["sda", "sdb"],
                            },
                        }
                    ],
                )
            ],
            default_system_id="qsosn-ha",
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
        self.assertEqual(payload["systems"][0]["ha_nodes"][1]["host"], "10.13.37.31")
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

    def test_adopt_removed_system_history_route_rehomes_orphaned_history(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/history/adopt-removed-system")
        settings = Settings(
            systems=[
                SystemConfig(
                    id="qsosn-ha",
                    label="QSOSN HA",
                    truenas=TrueNASConfig(
                        host="https://10.13.37.40",
                        platform="quantastor",
                    ),
                )
            ],
            default_system_id="qsosn-ha",
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
            "target_system_id": "qsosn-ha",
            "target_system_label": "QSOSN HA",
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
                            target_system_id="qsosn-ha",
                        )
                    )
                )

        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_rows"], 9)
        self.assertEqual(payload["target_system_id"], "qsosn-ha")
        self.assertEqual(payload["orphaned_systems"], [])
        history_store.adopt_system_history.assert_called_once_with(
            "qs-cryostorage",
            "qsosn-ha",
            target_system_label="QSOSN HA",
        )

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
                {"id": "node-a", "name": "QSOSN Left", "hostname": "10.13.37.30", "storageSystemClusterId": "cluster-a"},
                {"id": "node-b", "name": "QSOSN Right", "hostname": "10.13.37.31", "storageSystemClusterId": "cluster-a", "isMaster": True},
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
        self.assertEqual(payload["nodes"][1]["host"], "10.13.37.31")

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
                        "sudo -n /usr/bin/sg_ses -p aes /dev/sg27",
                        "sudo -n /usr/bin/sg_ses -p ec /dev/sg38",
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
        self.assertNotIn("/usr/sbin/zpool status -gP", payload["content"])

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
        self.assertEqual(payload["filename"], "truenas-jbod-ui-readonly")
        self.assertIn("skip writing a sudoers file", payload["detail"])
        self.assertIn("# Sudo rules disabled", payload["content"])

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
