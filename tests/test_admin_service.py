from __future__ import annotations

import asyncio
import json
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Request

from admin_service.config import AdminSettings
from admin_service.main import app as admin_app
from admin_service.main import build_admin_state_payload
from admin_service.main import decode_optional_secret_header
from app.config import (
    AdminSurfaceConfig,
    PathConfig,
    SSHConfig,
    Settings,
    SystemConfig,
    TrueNASConfig,
)
from app.main import app as main_app
from app.main import resolve_admin_launch_url
from app.models.domain import SystemSetupSudoPreviewRequest
from app.models.domain import EnclosureOption
from history_service.config import HistorySettings
from history_service.main import app as history_app


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
    def test_admin_sidecar_exposes_one_time_bootstrap_route(self) -> None:
        paths = {route.path for route in admin_app.routes}

        self.assertIn("/api/admin/system-setup/bootstrap", paths)
        self.assertIn("/api/admin/system-setup/sudoers-preview", paths)

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
        custom_profile = next(profile for profile in payload["profiles"] if profile["id"] == "lab-4x4")
        self.assertEqual(custom_profile["slot_count"], 16)
        self.assertIn("core", payload["setup_platform_defaults"])
        self.assertEqual(payload["ssh_keys"][0]["name"], "id_truenas")
        self.assertEqual(payload["paths"]["history_db"], "/tmp/history/history.db")
        self.assertEqual(payload["paths"]["tls_dir"], str(Path("C:/tmp/config") / "tls"))

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
        service.get_storage_view_candidates.assert_awaited_once_with(force_refresh=True)

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
