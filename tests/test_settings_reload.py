"""Main UI applies config.yaml / runtime-overrides.yaml edits without a restart (#432)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from app import config as app_config
from app.config import (
    RESTART_ONLY_SETTINGS,
    Settings,
    get_settings,
    restart_only_changes,
)
from app.settings_reload import (
    config_reload_problems,
    ConfigReloadMiddleware,
    ConfigReloader,
    PUBLIC_RELOAD_FAILURE,
    SettingsRuntime,
    file_signature,
)
from app import route_support
from app.services.inventory_registry import InventoryRegistry


def _system(system_id: str, label: str, host: str = "https://198.51.100.10") -> dict[str, Any]:
    return {
        "id": system_id,
        "label": label,
        "truenas": {"host": host, "api_key": "synthetic-key", "platform": "scale"},
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ConfigReloadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        self.config_path = self.root / "config" / "config.yaml"
        self.overrides_path = self.root / "config" / "runtime-overrides.yaml"
        self.config: dict[str, Any] = {
            "systems": [_system("alpha", "Alpha Shelf"), _system("beta", "Beta Shelf", "https://198.51.100.11")],
            "default_system_id": "alpha",
        }
        self._write_config()
        env = patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)})
        env.start()
        self.addCleanup(env.stop)
        for name in [name for name in os.environ if name.startswith(("TRUENAS_", "SSH_", "APP_")) and name != "APP_CONFIG_PATH"]:
            patcher = patch.dict(os.environ, {}, clear=False)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(name, None)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        self.clock = _Clock()
        self.runtime = SettingsRuntime()
        self.applied: list[tuple[Settings, Settings]] = []
        self.reloader = ConfigReloader(
            self.runtime,
            interval_seconds=2.0,
            clock=self.clock,
            on_applied=lambda before, after: self.applied.append((before, after)),
        )
        self.reloader.prime()

    def _write_config(self, text: str | None = None) -> None:
        # Write to a new file and rename it into place, like the admin service.
        temp = self.config_path.with_suffix(".tmp")
        temp.write_text(text if text is not None else yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        temp.replace(self.config_path)

    def _check(self) -> bool:
        self.clock.now += 5
        return asyncio.run(self.reloader.check_now())

    def _labels(self) -> dict[str, str | None]:
        return {system.id: system.label for system in self.runtime.current().settings.systems}


class ReloadAppliesChangesTests(ConfigReloadTestCase):
    def test_rename_is_applied_without_a_restart(self) -> None:
        first = self.runtime.current()
        self.config["systems"][0]["label"] = "Alpha Renamed"
        self._write_config()

        self.assertTrue(self._check())

        second = self.runtime.current()
        self.assertEqual(second.number, first.number + 1)
        self.assertEqual(self._labels()["alpha"], "Alpha Renamed")
        self.assertIsNone(self.reloader.problem)
        self.assertEqual(self.reloader.restart_pending, ())
        self.assertEqual(len(self.applied), 1)
        # The old generation still holds the old settings for requests pinned to it.
        self.assertEqual(first.settings.systems[0].label, "Alpha Shelf")

    def test_storage_view_rename_is_applied(self) -> None:
        self.config["systems"][0]["storage_views"] = [
            {"id": "boot", "label": "Boot SSDs", "kind": "boot_devices", "template_id": "satadom-pair-2"},
        ]
        self._write_config()
        self.assertTrue(self._check())
        self.config["systems"][0]["storage_views"][0]["label"] = "Boot mirror"
        self._write_config()
        self.assertTrue(self._check())
        views = self.runtime.current().settings.systems[0].storage_views
        self.assertEqual([view.label for view in views], ["Boot mirror"])

    def test_runtime_overrides_edit_is_applied(self) -> None:
        self.overrides_path.write_text(yaml.safe_dump({"app": {"smart_cache_ttl_seconds": 1234}}), encoding="utf-8")
        self.assertTrue(self._check())
        self.assertEqual(self.runtime.current().settings.app.smart_cache_ttl_seconds, 1234)

    def test_hand_edit_in_place_is_applied(self) -> None:
        # An editor that rewrites the same inode must still be noticed.
        inode = self.config_path.stat().st_ino
        self.config["systems"][1]["label"] = "Beta by hand"
        with self.config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(self.config, handle, sort_keys=False)
        self.assertEqual(self.config_path.stat().st_ino, inode)
        self.assertTrue(self._check())
        self.assertEqual(self._labels()["beta"], "Beta by hand")

    def test_unchanged_files_are_not_reloaded(self) -> None:
        with patch.object(self.reloader, "_loader", side_effect=AssertionError("must not reload")):
            self.assertFalse(self._check())

    def test_checks_are_rate_limited(self) -> None:
        calls: list[int] = []

        async def fake_check() -> bool:
            calls.append(1)
            return False

        async def run() -> None:
            with patch.object(self.reloader, "check_now", side_effect=fake_check):
                self.clock.now += 10
                await self.reloader.maybe_reload()
                await self.reloader.maybe_reload()
                self.clock.now += 1
                await self.reloader.maybe_reload()
                self.clock.now += 1.5
                await self.reloader.maybe_reload()

        asyncio.run(run())
        self.assertEqual(len(calls), 2)

    def test_file_signature_uses_mtime_size_and_inode(self) -> None:
        (entry,) = file_signature((self.config_path,))
        stat = self.config_path.stat()
        self.assertEqual(entry, (str(self.config_path), (stat.st_mtime_ns, stat.st_size, stat.st_ino)))
        self.assertEqual(file_signature((self.root / "missing.yaml",)), ((str(self.root / "missing.yaml"), None),))


class InvalidEditTests(ConfigReloadTestCase):
    def test_invalid_yaml_keeps_old_settings_with_a_warning(self) -> None:
        before = self.runtime.current()
        self._write_config("systems: [\n  - id: alpha\n    label: [unclosed\n")

        with self.assertLogs("app.settings_reload", level="WARNING") as logs:
            self.assertFalse(self._check())

        self.assertIs(self.runtime.current(), before)
        self.assertEqual(self._labels()["alpha"], "Alpha Shelf")
        self.assertTrue(self.reloader.problem.startswith("Config change not applied:"))
        self.assertIn("keeps the previous settings", self.reloader.problem)
        self.assertEqual(self.reloader.problems(), [self.reloader.problem])
        self.assertIn("Config change not applied", "\n".join(logs.output))

    def test_schema_error_names_the_setting_in_the_log_only(self) -> None:
        self.config["systems"][0]["truenas"]["platform"] = "not-a-platform"
        self._write_config()
        with self.assertLogs("app.settings_reload", level="WARNING") as logs:
            self.assertFalse(self._check())
        self.assertEqual(self._labels()["alpha"], "Alpha Shelf")
        self.assertIn("systems[0].truenas.platform", "\n".join(logs.output))
        self.assertEqual(self.reloader.problem, PUBLIC_RELOAD_FAILURE)

    def test_public_warning_never_quotes_file_content(self) -> None:
        """A YAML parser error quotes the bad line; that must reach neither the page nor /healthz."""
        secret = "synthetic-credential-7f3a"
        self._write_config(
            "systems:\n  - id: alpha\n    truenas:\n      api_key: \"" + secret + "\n      host: [\n"
        )
        with self.assertLogs("app.settings_reload", level="WARNING") as logs:
            self.assertFalse(self._check())
        self.assertEqual(self.reloader.problem, PUBLIC_RELOAD_FAILURE)
        self.assertNotIn(secret, self.reloader.problem)
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertIn("line", "\n".join(logs.output))

    def test_same_invalid_file_is_not_reparsed_or_relogged(self) -> None:
        self._write_config("- not a mapping\n")
        self.assertFalse(self._check())
        with patch.object(self.reloader, "_loader", side_effect=AssertionError("must not reload")):
            self.assertFalse(self._check())

    def test_fixing_the_file_clears_the_warning(self) -> None:
        self._write_config("- not a mapping\n")
        self._check()
        self.assertIsNotNone(self.reloader.problem)
        self.config["systems"][0]["label"] = "Alpha Fixed"
        self._write_config()
        self.assertTrue(self._check())
        self.assertIsNone(self.reloader.problem)
        self.assertEqual(self._labels()["alpha"], "Alpha Fixed")

    def test_unexpected_loader_failure_never_raises(self) -> None:
        self.config["systems"][0]["label"] = "Alpha Two"
        self._write_config()
        with patch.object(self.reloader, "_load", side_effect=RuntimeError("synthetic")):
            with self.assertLogs("app.settings_reload", level="WARNING") as logs:
                self.assertFalse(self._check())
        self.assertEqual(self.reloader.problem, PUBLIC_RELOAD_FAILURE)
        self.assertIn("RuntimeError", "\n".join(logs.output))


class RestartOnlySettingsTests(ConfigReloadTestCase):
    def test_restart_only_keys_are_named_and_keep_running_values(self) -> None:
        running = self.runtime.current().settings
        self.config["app"] = {
            "public_origin": "https://ui.example.test",
            "debug": True,
            "refresh_interval_seconds": 45,
        }
        self.config["systems"][0]["label"] = "Alpha Renamed"
        self._write_config()

        with self.assertLogs("app.settings_reload", level="WARNING") as logs:
            self.assertTrue(self._check())

        current = self.runtime.current().settings
        self.assertEqual(self.reloader.restart_pending, ("app.public_origin", "app.debug"))
        self.assertIn("needs a main UI restart", "\n".join(logs.output))
        # Restart-only values stay as the process started with them...
        self.assertEqual(current.app.public_origin, running.app.public_origin)
        self.assertEqual(current.app.debug, running.app.debug)
        # ...and everything else is applied.
        self.assertEqual(current.app.refresh_interval_seconds, 45)
        self.assertEqual(self._labels()["alpha"], "Alpha Renamed")

    def test_restart_only_change_alone_does_not_swap(self) -> None:
        before = self.runtime.current()
        self.config["app"] = {"public_origin": "https://nas.example.test"}
        self._write_config()
        self.assertFalse(self._check())
        self.assertIs(self.runtime.current(), before)
        self.assertEqual(self.reloader.restart_pending, ("app.public_origin",))

    def test_restart_only_changes_lists_every_declared_key(self) -> None:
        before = Settings()
        after = before.model_copy(
            update={
                "app": before.app.model_copy(
                    update={"public_origin": "https://ui.example.test", "release_check_enabled": False}
                ),
                "perf": before.perf.model_copy(update={"enabled": True}),
                "paths": before.paths.model_copy(update={"log_file": "/tmp/elsewhere.log"}),
            }
        )
        self.assertEqual(
            restart_only_changes(before, after),
            ["app.public_origin", "app.release_check_enabled", "perf", "paths"],
        )
        renamed = before.model_copy(update={"systems": [], "default_system_id": "x"})
        self.assertEqual(restart_only_changes(before, renamed), [])
        self.assertIn(("app", "public_origin"), RESTART_ONLY_SETTINGS)


class ConcurrencyTests(ConfigReloadTestCase):
    def test_request_during_swap_sees_one_generation(self) -> None:
        """A request that started before a reload keeps the old settings and components."""
        from app import route_support

        runtime = SettingsRuntime()
        reloader = ConfigReloader(runtime, interval_seconds=0.0, clock=self.clock)
        reloader.prime()
        observed: dict[str, Any] = {}
        entered = asyncio.Event
        release_holder: dict[str, asyncio.Event] = {}

        async def slow_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            first = route_support.get_settings()
            registry = route_support.get_inventory_registry()
            release_holder["started"].set()
            await release_holder["release"].wait()
            observed[scope["path"]] = (
                first.systems[0].label,
                route_support.get_settings().systems[0].label,
                registry.settings.systems[0].label,
                route_support.get_inventory_registry().settings.systems[0].label,
                route_support.get_inventory_registry() is registry,
            )

        middleware = ConfigReloadMiddleware(slow_app, reloader=reloader)

        async def run() -> None:
            release_holder["started"] = entered()
            release_holder["release"] = entered()
            old_request = asyncio.create_task(middleware({"type": "http", "path": "/old"}, None, None))
            await release_holder["started"].wait()
            self.config["systems"][0]["label"] = "Alpha New"
            self._write_config()
            self.clock.now += 5
            release_holder["started"] = entered()
            new_request = asyncio.create_task(middleware({"type": "http", "path": "/new"}, None, None))
            await release_holder["started"].wait()
            release_holder["release"].set()
            await asyncio.gather(old_request, new_request)

        with patch.object(route_support, "SETTINGS_RUNTIME", runtime):
            asyncio.run(run())
        self.assertEqual(observed["/old"], ("Alpha Shelf",) * 4 + (True,))
        self.assertEqual(observed["/new"], ("Alpha New",) * 4 + (True,))

    def test_threads_never_see_a_half_built_settings_object(self) -> None:
        runtime = SettingsRuntime()
        seen: set[tuple[str | None, str | None]] = set()
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                settings = runtime.current().settings
                seen.add((settings.systems[0].label, settings.systems[1].label))

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for index in range(20):
                self.config["systems"][0]["label"] = f"Alpha {index}"
                self.config["systems"][1]["label"] = f"Beta {index}"
                self._write_config()
                self.clock.now += 5
                asyncio.run(self.reloader.check_now())
        finally:
            stop.set()
            for thread in threads:
                thread.join()
        for alpha, beta in seen:
            if alpha == "Alpha Shelf":
                self.assertEqual(beta, "Beta Shelf")
            else:
                self.assertEqual(alpha.split()[1], beta.split()[1])


class RegistryCarryOverTests(ConfigReloadTestCase):
    def test_rename_keeps_appliance_cache_and_new_label(self) -> None:
        old_settings = self.runtime.current().settings
        old_registry = InventoryRegistry(old_settings)
        old_service = old_registry.get_service("alpha")
        sentinel_bundle = object()
        old_service._source_bundle = sentinel_bundle  # type: ignore[assignment]

        self.config["systems"][0]["label"] = "Alpha Renamed"
        self._write_config()
        self._check()
        new_registry = InventoryRegistry(self.runtime.current().settings, previous=old_registry)
        new_service = new_registry.get_service("alpha")

        self.assertIsNot(new_service, old_service)
        self.assertIs(new_service._source_bundle, sentinel_bundle)
        self.assertEqual(new_service.system.label, "Alpha Renamed")
        self.assertEqual(old_service.system.label, "Alpha Shelf")
        self.assertIs(new_registry.mapping_store, old_registry.mapping_store)
        self.assertIs(new_service._disk_inventory_sync_lock, old_service._disk_inventory_sync_lock)

    def test_layout_change_drops_the_raw_answers(self) -> None:
        old_registry = InventoryRegistry(self.runtime.current().settings)
        old_registry.get_service("alpha")._source_bundle = object()  # type: ignore[assignment]
        self.config["layout"] = {"slot_count": 12, "rows": 3, "columns": 4}
        self._write_config()
        self.assertTrue(self._check())
        new_service = InventoryRegistry(self.runtime.current().settings, previous=old_registry).get_service("alpha")
        self.assertIsNone(new_service._source_bundle)
        self.assertEqual(new_service._cache, {})

    def test_lowered_ttls_cap_carried_expiry(self) -> None:
        from datetime import timedelta

        from app.services.inventory import utcnow

        old_registry = InventoryRegistry(self.runtime.current().settings)
        old_service = old_registry.get_service("alpha")
        far = utcnow() + timedelta(hours=20)
        old_service._source_bundle = object()  # type: ignore[assignment]
        old_service._source_bundle_until = far
        old_service._sg_ses_device_cache = {"host": (["/dev/sg1"], far)}
        self.overrides_path.write_text(
            yaml.safe_dump({"app": {"source_bundle_cache_ttl_seconds": 0, "sg_ses_device_cache_ttl_seconds": 60}}),
            encoding="utf-8",
        )
        self.assertTrue(self._check())
        new_service = InventoryRegistry(self.runtime.current().settings, previous=old_registry).get_service("alpha")
        self.assertLessEqual(new_service._source_bundle_until, utcnow())
        (_devices, until) = new_service._sg_ses_device_cache["host"]
        self.assertLessEqual(until, utcnow() + timedelta(seconds=61))

    def test_connection_change_drops_the_cache(self) -> None:
        old_registry = InventoryRegistry(self.runtime.current().settings)
        old_service = old_registry.get_service("beta")
        old_service._source_bundle = object()  # type: ignore[assignment]

        self.config["systems"][1]["truenas"]["host"] = "https://198.51.100.99"
        self._write_config()
        self._check()
        new_service = InventoryRegistry(self.runtime.current().settings, previous=old_registry).get_service("beta")
        self.assertIsNone(new_service._source_bundle)

    def test_removed_system_is_not_configured_after_reload(self) -> None:
        from app.services.inventory_registry import SystemNotConfiguredError

        old_registry = InventoryRegistry(self.runtime.current().settings)
        old_registry.get_service("beta")
        del self.config["systems"][1]
        self._write_config()
        self._check()
        new_registry = InventoryRegistry(self.runtime.current().settings, previous=old_registry)
        self.assertFalse(new_registry.has_system("beta"))
        with self.assertRaises(SystemNotConfiguredError):
            new_registry.get_service("beta")


class MainAppWiringTests(ConfigReloadTestCase):
    def _app(self) -> Any:
        from app import main as app_main
        from app import route_support

        runtime = SettingsRuntime()
        patcher = patch.object(route_support, "SETTINGS_RUNTIME", runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        application = app_main.create_app()
        reloader = application.state.config_reloader
        reloader.runtime = runtime
        reloader._clock = self.clock
        reloader.prime()
        return app_main, application

    def test_app_generation_follows_reload_and_healthz_reports_invalid_edit(self) -> None:
        app_main, application = self._app()
        registry_before = route_support.get_inventory_registry()
        self.config["systems"][0]["label"] = "Alpha Renamed"
        self._write_config()
        self.clock.now += 5
        asyncio.run(application.state.config_reloader.check_now())
        registry_after = route_support.get_inventory_registry()
        self.assertIsNot(registry_after, registry_before)
        self.assertEqual(registry_after.get_system("alpha").label, "Alpha Renamed")

        self._write_config("- not a mapping\n")
        self.clock.now += 5
        with self.assertLogs("app.settings_reload", level="WARNING"):
            asyncio.run(application.state.config_reloader.check_now())
        self.assertIs(route_support.get_inventory_registry(), registry_after)
        request = type("R", (), {"app": application})()
        problems = config_reload_problems(request)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("Config change not applied:"))
        payload = route_support.build_health_payload(None, remote_problems=problems)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(route_support.health_status_code(payload), 200)
        self.assertTrue(str(payload["summary"]).startswith("Config change not applied:"))

    def test_restart_notice_is_a_page_warning_not_a_health_problem(self) -> None:
        app_main, application = self._app()
        self.config["app"] = {"public_origin": "https://ui.example.test"}
        self._write_config()
        self.clock.now += 5
        with self.assertLogs("app.settings_reload", level="WARNING"):
            asyncio.run(application.state.config_reloader.check_now())
        request = type("R", (), {"app": application})()
        self.assertEqual(
            config_reload_problems(request),
            ["Config change to app.public_origin is saved but needs a main UI restart to take effect."],
        )
        self.assertEqual(config_reload_problems(request, include_restart_notice=False), [])

    def test_inventory_route_carries_the_reload_warning(self) -> None:
        app_main, application = self._app()
        self._write_config("- not a mapping\n")
        self.clock.now += 5
        with self.assertLogs("app.settings_reload", level="WARNING"):
            asyncio.run(application.state.config_reloader.check_now())
        route = next(route for route in application.routes if getattr(route, "path", "") == "/api/inventory")

        class _Service:
            system = type("S", (), {"id": "alpha", "truenas": type("T", (), {"platform": "scale"})()})()

            async def get_snapshot(self, **_: Any) -> Any:
                from app.models.domain import InventorySnapshot

                return InventorySnapshot(slots=[], refresh_interval_seconds=30, warnings=["existing"])

        registry = type("Reg", (), {"get_service": lambda self, _id: _Service(), "has_system": lambda self, _id: True})()
        request = type(
            "Req",
            (),
            {"app": application, "url": type("U", (), {"scheme": "http", "netloc": "ui.example.test"})(), "headers": {}},
        )()
        with patch("app.routes.get_inventory_registry", return_value=registry):
            response = asyncio.run(route.endpoint(request=request, force=False, system_id=None, enclosure_id=None))
        warnings = json.loads(response.body)["warnings"]
        self.assertTrue(warnings[0].startswith("Config change not applied:"))
        self.assertEqual(warnings[1], "existing")


class ModuleCacheCompatibilityTests(unittest.TestCase):
    def test_get_settings_cache_clear_still_forces_a_fresh_load(self) -> None:
        first = app_config.get_settings()
        self.assertIs(app_config.get_settings(), first)
        app_config.get_settings.cache_clear()
        self.assertIsNot(app_config.get_settings(), first)
        app_config.get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
