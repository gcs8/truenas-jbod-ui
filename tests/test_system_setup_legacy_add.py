"""Adding a system next to a legacy top-level system must not drop it (#1227).

A config without a nonempty ``systems`` list still monitors one system built
from the top-level ``truenas`` and ``ssh`` sections (and their environment
overrides). Writing a ``systems`` list replaces that implicit system, so an
add that starts the list from empty silently stops monitoring it. System Setup
refuses that add before writing and names the migration step; saving the
legacy system itself under its own id is that migration and stays allowed.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml
from fastapi import FastAPI

import admin_service.routes as admin_routes
from admin_service.config import AdminSettings
from app.config import get_settings, load_settings
from app.models.domain import DemoSystemRequest, SystemSetupRequest
from app.router_inclusion import include_router_preserving_route_objects
from app.services.demo_system_factory import DemoSystemFactory
from app.services.system_setup import SystemSetupService

# The fixed refusal, pinned here so a reworded message is a reviewed change.
LEGACY_SYSTEM_MIGRATION_DETAIL = (
    "This config still monitors a legacy system ('default') from its top-level "
    "truenas and ssh settings in config.yaml or .env, and adding another system now "
    "would stop monitoring it. Save the 'default' system in System Setup first so it "
    "moves into the systems list, then add this one."
)

# Environment overrides that feed the implicit legacy system, plus the config
# path. Each test starts with none of them set so the host shell cannot leak in.
_SYSTEM_ENV = (
    "APP_CONFIG_PATH",
    "TRUENAS_HOST",
    "SSH_HOST",
    "SYSTEM_DEFAULT_ID",
    "BACKUP_CONFIG_ENABLED",
)

LEGACY_CONFIG = {
    "truenas": {"host": "https://legacy.example.test", "platform": "core"},
    "ssh": {"enabled": True, "host": "legacy.example.test", "user": "jbodmap"},
}


async def _post(app: FastAPI, path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    messages: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()
        return {}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"localhost"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 80),
        },
        receive,
        send,
    )
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(raw)


class _LegacyConfigMixin:
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config" / "config.yaml"
        self.config_path.parent.mkdir()
        environ = {key: value for key, value in os.environ.items() if key not in _SYSTEM_ENV}
        environ["APP_CONFIG_PATH"] = str(self.config_path)
        environ["BACKUP_CONFIG_ENABLED"] = "false"
        env_patch = mock.patch.dict(os.environ, environ, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)

    def _write(self, document: dict) -> bytes:
        self.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return self.config_path.read_bytes()

    def _effective_system_hosts(self) -> dict[str, str]:
        settings = load_settings(create_directories=False)
        return {system.id: system.truenas.host for system in settings.systems}

    @staticmethod
    def _request(system_id: str, host: str = "https://new.example.test") -> SystemSetupRequest:
        return SystemSetupRequest(system_id=system_id, label=system_id, truenas_host=host)


class LegacySystemAddRouteTests(_LegacyConfigMixin, unittest.TestCase):
    """The registered admin route, real config load and real config writer."""

    def setUp(self) -> None:
        super().setUp()
        self.app = FastAPI()
        include_router_preserving_route_objects(self.app, admin_routes.build_router(AdminSettings()))

        async def no_runtime(*args, **kwargs):
            return {"available": False, "containers": []}

        # Only runtime-control reporting is replaced, so no container socket is reached.
        for patcher in (
            mock.patch.object(
                admin_routes,
                "get_runtime_service",
                lambda: SimpleNamespace(mark_restart_required=lambda keys: None),
            ),
            mock.patch.object(admin_routes, "build_runtime_payload", no_runtime),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post_setup(self, payload: dict) -> tuple[int, dict]:
        return asyncio.run(_post(self.app, "/api/admin/system-setup", payload))

    def test_adding_a_system_to_a_legacy_config_is_refused_and_writes_nothing(self) -> None:
        before_bytes = self._write(LEGACY_CONFIG)
        self.assertEqual(self._effective_system_hosts(), {"default": "https://legacy.example.test"})

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )

        self.assertEqual(status, 400, response)
        self.assertEqual(response.get("detail"), LEGACY_SYSTEM_MIGRATION_DETAIL)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)
        self.assertEqual(self._effective_system_hosts(), {"default": "https://legacy.example.test"})

    def test_saving_the_legacy_system_then_adding_keeps_both_monitored(self) -> None:
        # Control: the explicit migration (saving the legacy system under its
        # own id) is accepted, and after it an add keeps both systems.
        self._write(LEGACY_CONFIG)

        status, response = self._post_setup(
            {"system_id": "default", "label": "Primary", "truenas_host": "https://legacy.example.test"}
        )
        self.assertEqual(status, 200, response)
        self.assertTrue(response["ok"])

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )
        self.assertEqual(status, 200, response)
        self.assertTrue(response["ok"])
        self.assertEqual(
            self._effective_system_hosts(),
            {"default": "https://legacy.example.test", "new-host": "https://new.example.test"},
        )

    def test_first_system_on_a_config_without_a_legacy_host_is_saved(self) -> None:
        # Control: a fresh install has no legacy host, so nothing is monitored
        # yet and the first add is the normal path.
        self._write({"app": {"log_level": "INFO"}})

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )

        self.assertEqual(status, 200, response)
        self.assertEqual(self._effective_system_hosts(), {"new-host": "https://new.example.test"})


class LegacySystemAddServiceTests(_LegacyConfigMixin, unittest.TestCase):
    """Every source of the implicit legacy system is protected, not only truenas.host."""

    def _assert_refused_unchanged(self, before_bytes: bytes | None) -> None:
        service = SystemSetupService(str(self.config_path))
        with self.assertRaises(ValueError) as caught:
            service.save_system(self._request("new-host"))
        self.assertEqual(str(caught.exception), LEGACY_SYSTEM_MIGRATION_DETAIL)
        if before_bytes is None:
            self.assertFalse(self.config_path.exists())
        else:
            self.assertEqual(self.config_path.read_bytes(), before_bytes)

    def test_ssh_only_legacy_system_is_refused(self) -> None:
        before = self._write(
            {
                "truenas": {"platform": "linux"},
                "ssh": {"enabled": True, "host": "linux.example.test", "user": "jbodmap"},
            }
        )
        self._assert_refused_unchanged(before)

    def test_legacy_system_from_environment_is_refused(self) -> None:
        os.environ["TRUENAS_HOST"] = "https://env.example.test"
        self.assertEqual(self._effective_system_hosts(), {"default": "https://env.example.test"})
        self._assert_refused_unchanged(None)

    def test_blank_environment_host_is_not_a_legacy_system(self) -> None:
        # Control: load_settings treats a blank override as unset.
        os.environ["TRUENAS_HOST"] = "   "
        SystemSetupService(str(self.config_path)).save_system(self._request("new-host"))
        self.assertEqual(self._effective_system_hosts(), {"new-host": "https://new.example.test"})

    def test_populated_systems_list_still_accepts_an_add(self) -> None:
        # Control: once systems exist the top-level sections are already
        # ignored, so an add changes nothing that was monitored.
        self._write(
            {
                **LEGACY_CONFIG,
                "systems": [{"id": "lab", "label": "Lab", "truenas": {"host": "https://lab.example.test"}}],
            }
        )
        SystemSetupService(str(self.config_path)).save_system(self._request("new-host"))
        self.assertEqual(
            self._effective_system_hosts(),
            {"lab": "https://lab.example.test", "new-host": "https://new.example.test"},
        )

    def test_demo_creation_uses_the_same_legacy_guard(self) -> None:
        before = self._write(
            {
                "truenas": {"platform": "linux"},
                "ssh": {"enabled": True, "host": "linux.example.test", "user": "jbodmap"},
            }
        )
        profile_path = self.root / "config" / "profiles.yaml"
        factory = DemoSystemFactory(str(self.config_path), str(profile_path))
        with self.assertRaises(ValueError) as caught:
            factory.create_demo_system(DemoSystemRequest(), load_settings(create_directories=False))
        self.assertIn("legacy system", str(caught.exception))
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(profile_path.exists())


if __name__ == "__main__":
    unittest.main()
