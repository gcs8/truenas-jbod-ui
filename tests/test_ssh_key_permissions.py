import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from fastapi import APIRouter

from admin_service import transport_routes
from app.config import Settings, SSHConfig, SystemConfig
from app.services.ssh_key_manager import SSHKeyManager
from tests.test_ui_health_and_admin_probe import _registry, _route, app_main, app_route_support, app_routes


class SSHKeyPermissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root / "configured-key"
        self.key.write_text("synthetic contents, not a key")
        self.ssh = SSHConfig(key_path=str(self.key))
        self.settings = Settings(config_file=str(self.root / "config.yaml"), ssh=self.ssh,
                                 systems=[SystemConfig(ssh=self.ssh)])
        self.enterContext(patch.dict(os.environ, APP_UID=str(os.getuid()), APP_GID=str(os.getgid())))

    def admin_list(self):
        router = APIRouter()
        transport_routes.add_transport_routes(router, SimpleNamespace(
            reload_app_settings=lambda: self.settings, SSHKeyManager=SSHKeyManager))
        route = next(route for route in router.routes if route.path == "/api/admin/ssh-keys")
        return json.loads(asyncio.run(route.endpoint()).body)

    def test_admin_list_uses_application_owner_and_group_not_reader_identity(self):
        with patch.dict(os.environ, APP_UID="21001", APP_GID="21002"):
            for uid, gid, mode, warns in (
                (21001, 77, 0o600, False), (21001, 21002, 0o640, False),
                (21001, 77, 0o640, True), (0, 21002, 0o640, False), (0, 77, 0o600, False),
                (21003, 21002, 0o600, True), (21001, 21002, 0o620, True),
                (21001, 21002, 0o644, True), (0, 21002, 0o602, True), (0, 21002, 0o601, True),
            ):
                with self.subTest(uid=uid, gid=gid, mode=oct(mode)), patch.object(
                    Path, "lstat", autospec=True,
                    return_value=SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=gid),
                ):
                    warnings = self.admin_list().get("ssh_key_warnings", [])
                    self.assertEqual(len(warnings), int(warns))
                    if warns:
                        self.assertIn(str(self.key), warnings[0])
                        self.assertIn("chmod 600", warnings[0])
                        self.assertIn("chmod 640", warnings[0])
                        self.assertNotIn("synthetic contents", warnings[0])

    def test_regular_keys_can_be_generated_and_listed_without_posix_open_flags(self):
        portable_os = SimpleNamespace(O_RDONLY=os.O_RDONLY, open=os.open, fdopen=os.fdopen,
                                      fstat=os.fstat, path=os.path)
        manager = SSHKeyManager(self.settings.config_file)
        with patch("app.services.ssh_key_manager.os", portable_os):
            generated = manager.generate_keypair("portable")
            self.assertEqual(manager.list_keys(), [generated])

    def test_configured_symlink_warns_without_offering_its_target_as_a_key(self):
        manager = SSHKeyManager(self.settings.config_file)
        generated = manager.generate_keypair("source")
        target = Path(generated["private_path"])
        link = manager.key_dir / "selected-link"
        link.symlink_to(target)
        self.ssh.key_path = str(link)
        result = self.admin_list()
        self.assertNotIn(link.name, [row["name"] for row in result["keys"]])
        self.assertIn(str(link), " ".join(result.get("ssh_key_warnings", [])))
        target.unlink()
        self.assertIn("symlink", " ".join(self.admin_list()["ssh_key_warnings"]).lower())

    def test_startup_health_reprobe_and_reload_follow_configured_paths_only(self):
        self.key.chmod(0o644)
        unrelated = self.root / "unconfigured-key"
        unrelated.touch(mode=0o644)
        service = Mock()
        service.peek_cached_snapshot.return_value = None
        with (patch.object(app_main, "get_settings", return_value=self.settings),
              patch.object(app_main, "probe_writable_directories", return_value=[]),
              patch.object(app_route_support, "probe_writable_directories", return_value=[]),
              patch.object(app_routes, "get_inventory_registry", return_value=_registry(service)),
              patch.object(app_routes, "history_service_problem", return_value=None),
              patch.object(app_routes, "backup_archive_problems", return_value=[]),
              patch.object(app_main.logger, "warning") as log):
            application = app_main.create_app()
            request = SimpleNamespace(app=application)

            def health():
                response = asyncio.run(_route("/healthz").endpoint(request))
                self.assertEqual(response.status_code, 200)
                return json.loads(response.body)

            body = health()
            self.assertEqual(body["status"], "degraded")
            self.assertEqual(len(body["problems"]), 1)
            self.assertIn(str(self.key), body["problems"][0])
            self.assertIn("chmod 600", body["problems"][0])
            self.assertIn(body["problems"][0], str(log.call_args_list))
            self.assertNotIn("synthetic contents", str(log.call_args_list))
            self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o644)
            self.key.chmod(0o600)
            application.state.storage_checked_at_monotonic = 0
            self.assertEqual(health()["status"], "ok")
            updated = self.settings.model_copy(update={"systems": [SystemConfig(ssh=SSHConfig(key_path=str(unrelated)))]})
            app_route_support.after_config_reload(application, self.settings, updated)
            self.assertIn(str(unrelated), " ".join(health()["problems"]))

    def test_both_services_receive_the_same_deployment_identity(self):
        for filename in ("docker-compose.yml", "docker-compose.dev.yml"):
            services = yaml.safe_load(Path(filename).read_text())["services"]
            for name in ("enclosure-admin", "enclosure-ui"):
                for variable in ("APP_UID", "APP_GID"):
                    with self.subTest(filename=filename, service=name, variable=variable):
                        self.assertEqual(services[name]["environment"].get(variable), "${" + variable + ":-10001}")
