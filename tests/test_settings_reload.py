"""Main UI applies config.yaml / runtime-overrides.yaml edits without a restart (#432)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
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
from app.models.domain import InventorySnapshot, SnapshotExportRequest
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

    def test_config_restored_unchanged_clears_missing_warning(self) -> None:
        before = self.runtime.current()
        moved = self.config_path.with_name("config.yaml.moved")
        self.config_path.rename(moved)
        with self.assertLogs("app.settings_reload", level="WARNING"):
            self.assertFalse(self._check())
        self.assertIsNotNone(self.reloader.problem)

        # Same inode, size and mtime: the signature equals the running baseline.
        moved.rename(self.config_path)
        self.assertFalse(self._check())
        self.assertIsNone(self.reloader.problem)
        self.assertIs(self.runtime.current(), before)

    def test_missing_config_after_start_keeps_last_valid_generation(self) -> None:
        before = self.runtime.current()
        self.config_path.unlink()

        with self.assertLogs("app.settings_reload", level="WARNING") as logs:
            self.assertFalse(self._check())

        self.assertIs(self.runtime.current(), before)
        self.assertEqual(self._labels()["alpha"], "Alpha Shelf")
        self.assertIsNotNone(self.reloader.problem)
        self.assertIn("config.yaml is missing", self.reloader.problem or "")
        self.assertIn("keeps the previous settings", self.reloader.problem or "")
        self.assertIn("config file is missing", "\n".join(logs.output).lower())

        self.config["systems"][0]["label"] = "Alpha Restored"
        self._write_config()
        self.assertTrue(self._check())
        self.assertEqual(self._labels()["alpha"], "Alpha Restored")
        self.assertIsNone(self.reloader.problem)

    def test_first_start_without_config_keeps_explicit_default_behavior(self) -> None:
        self.config_path.unlink()
        get_settings.cache_clear()
        startup = app_config.load_settings()
        runtime = SettingsRuntime(lambda: startup)
        reloader = ConfigReloader(runtime, interval_seconds=0.0, clock=self.clock)

        reloader.prime(startup)

        self.assertFalse(asyncio.run(reloader.check_now()))
        self.assertIsNone(reloader.problem)
        self.assertEqual(runtime.current().settings.config_file, str(self.config_path))

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

    def test_pending_profile_file_path_does_not_supply_live_profiles(self) -> None:
        running_profile_path = self.root / "config" / "profiles.yaml"
        pending_profile_path = self.root / "config" / "pending-profiles.yaml"
        running_profile_path.write_text(
            yaml.safe_dump({"profiles": [{"id": "running-profile", "label": "Running", "rows": 1, "columns": 1}]}),
            encoding="utf-8",
        )
        pending_profile_path.write_text(
            yaml.safe_dump({"profiles": [{"id": "pending-profile", "label": "Pending", "rows": 1, "columns": 1}]}),
            encoding="utf-8",
        )
        get_settings.cache_clear()
        runtime = SettingsRuntime()
        reloader = ConfigReloader(runtime, interval_seconds=0.0, clock=self.clock)
        reloader.prime()
        self.assertEqual([profile.id for profile in runtime.current().settings.profiles], ["running-profile"])

        self.config["paths"] = {"profile_file": str(pending_profile_path)}
        self.config["systems"][0]["label"] = "Alpha Renamed"
        self._write_config()

        self.assertTrue(asyncio.run(reloader.check_now()))

        current = runtime.current().settings
        self.assertEqual(current.paths.profile_file, str(running_profile_path))
        self.assertEqual([profile.id for profile in current.profiles], ["running-profile"])
        self.assertEqual(reloader.restart_pending, ("paths",))

    def test_pending_profile_file_that_would_break_restart_is_rejected(self) -> None:
        running_profile_path = self.root / "config" / "profiles.yaml"
        running_profile_path.write_text(
            yaml.safe_dump({"profiles": [{"id": "running-profile", "label": "Running", "rows": 1, "columns": 1}]}),
            encoding="utf-8",
        )
        pending_profile_path = self.root / "config" / "pending-profiles.yaml"
        broken_contents = {
            "malformed yaml": "profiles: [unclosed\n",
            "wrong shape": yaml.safe_dump({"profiles": "not-a-list"}),
            "invalid profile": yaml.safe_dump({"profiles": [{"id": "bad", "rows": "many"}]}),
        }
        for label, contents in broken_contents.items():
            with self.subTest(label):
                pending_profile_path.write_text(contents, encoding="utf-8")
                self.config["paths"] = {}
                self._write_config()
                get_settings.cache_clear()
                runtime = SettingsRuntime()
                reloader = ConfigReloader(runtime, interval_seconds=0.0, clock=self.clock)
                reloader.prime()
                before = runtime.current()

                self.config["paths"] = {"profile_file": str(pending_profile_path)}
                self.config["systems"][0]["label"] = f"Alpha {label}"
                self._write_config()
                with self.assertLogs("app.settings_reload", level="WARNING"):
                    self.assertFalse(asyncio.run(reloader.check_now()))

                self.assertIs(runtime.current(), before)
                self.assertIsNotNone(reloader.problem)
                self.assertEqual(reloader.restart_pending, ())

    def test_scalar_inline_profiles_is_a_configuration_error(self) -> None:
        from app.config import load_settings
        from app.config_errors import ConfigurationError

        profile_path = self.root / "config" / "profiles.yaml"
        for with_profile_file in (False, True):
            with self.subTest(with_profile_file=with_profile_file):
                if with_profile_file:
                    profile_path.write_text(yaml.safe_dump({"profiles": []}), encoding="utf-8")
                else:
                    profile_path.unlink(missing_ok=True)
                self.config["profiles"] = 5
                self._write_config()
                with self.assertRaises(ConfigurationError):
                    load_settings()

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
    def test_pre_reload_export_cannot_repopulate_the_new_generation_cache(self) -> None:
        route_support.SNAPSHOT_EXPORT_SOURCE_CACHE.clear()
        self.addCleanup(route_support.SNAPSHOT_EXPORT_SOURCE_CACHE.clear)
        runtime = SettingsRuntime()
        app = SimpleNamespace(state=SimpleNamespace())
        reloader = ConfigReloader(
            runtime,
            interval_seconds=0.0,
            clock=self.clock,
            on_applied=lambda before, after: route_support.after_config_reload(app, before, after),
        )
        reloader.prime()
        old_generation = runtime.current()
        old_settings = old_generation.settings

        class SlowService:
            system = SimpleNamespace(id="alpha")

            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def get_snapshot(self, **_: Any) -> InventorySnapshot:
                self.started.set()
                await self.release.wait()
                return InventorySnapshot(slots=[], refresh_interval_seconds=30)

            async def get_slot_smart_summaries(self, *_: Any, **__: Any) -> list[Any]:
                return []

        service = SlowService()

        async def run() -> None:
            with runtime.pin(old_generation):
                old_export = asyncio.create_task(
                    route_support._load_snapshot_export_source(
                        service=service,
                        payload=SnapshotExportRequest(),
                        enclosure_id=None,
                        stage_prefix="test.export",
                        settings=old_settings,
                    )
                )
            await service.started.wait()
            self.config["systems"][0]["label"] = "Alpha New"
            self._write_config()
            self.assertTrue(await reloader.check_now())
            self.assertEqual(route_support.SNAPSHOT_EXPORT_SOURCE_CACHE, {})
            service.release.set()
            await old_export

        with patch.object(route_support, "SETTINGS_RUNTIME", runtime):
            asyncio.run(run())

        self.assertEqual(
            route_support.SNAPSHOT_EXPORT_SOURCE_CACHE,
            {},
            "work pinned to the old generation must not publish after reload invalidation",
        )

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

    def test_invalid_log_level_keeps_inventory_usable_through_real_asgi(self) -> None:
        import logging

        # Keep the real settings, middleware, logging builder and routes. Only
        # appliance inventory is synthetic; no lifespan or outbound I/O runs.
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        previous_handlers = tuple(root_logger.handlers)

        def restore_logging() -> None:
            root_logger.setLevel(previous_level)
            for handler in tuple(root_logger.handlers):
                if handler not in previous_handlers:
                    root_logger.removeHandler(handler)
                    handler.close()

        self.addCleanup(restore_logging)
        _main, application = self._app()
        reloader = application.state.config_reloader
        runtime = reloader.runtime
        built: list[Any] = []

        class SyntheticRegistry:
            def __init__(self, settings: Settings, **_: Any) -> None:
                self.settings = settings
                self.system = settings.systems[0]
                built.append(self)

            def get_service(self, _system_id: Any) -> Any:
                return self

            async def get_snapshot(self, **_: Any) -> InventorySnapshot:
                return InventorySnapshot(slots=[], refresh_interval_seconds=self.settings.app.refresh_interval_seconds)

            def peek_cached_snapshot(self) -> None:
                return None

        async def get(path: str) -> tuple[int, dict[str, Any]]:
            messages: list[dict[str, Any]] = []

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message: dict[str, Any]) -> None:
                messages.append(message)

            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": path, "raw_path": path.encode(), "query_string": b"",
                "headers": [(b"host", b"ui.example.test")], "root_path": "",
                "client": ("192.0.2.1", 1234), "server": ("ui.example.test", 80),
            }
            try:
                await application(scope, receive, send)
            except Exception:
                # Starlette sends the 500 then re-raises to its ASGI server.
                if not any(item.get("status") == 500 for item in messages):
                    raise
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
            return status, json.loads(body)

        async def run() -> None:
            status, inventory = await get("/api/inventory")
            self.assertEqual(status, 200)
            self.assertEqual(inventory["refresh_interval_seconds"], 30)
            original = runtime.current()
            registry = route_support.get_inventory_registry()
            self.config["app"] = {"log_level": "unsupported-synthetic-level", "refresh_interval_seconds": 55}
            self._write_config()
            self.clock.now += 5
            status, inventory = await get("/api/inventory")
            self.assertEqual(status, 200)
            self.assertIs(runtime.current(), original)
            self.assertIs(route_support.get_inventory_registry(), registry)
            self.assertEqual(inventory["refresh_interval_seconds"], 30)
            self.assertEqual(inventory["warnings"], [PUBLIC_RELOAD_FAILURE])
            status, health = await get("/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["problems"], [PUBLIC_RELOAD_FAILURE])
            self.assertEqual((await get("/livez"))[0], 200)
            self.assertEqual(len(built), 1)
            # The same rejected signature does not create warnings repeatedly.
            self.clock.now += 5
            self.assertEqual((await get("/api/inventory"))[1]["warnings"], [PUBLIC_RELOAD_FAILURE])
            self.config["app"] = {"log_level": "info", "refresh_interval_seconds": 55}
            self._write_config()
            self.clock.now += 5
            status, inventory = await get("/api/inventory")
            self.assertEqual(status, 200)
            self.assertEqual(inventory["refresh_interval_seconds"], 55)
            self.assertEqual(inventory["warnings"], [])
            self.assertEqual(runtime.current().number, original.number + 1)
            self.assertEqual(len(built), 2)

        with patch.object(route_support, "InventoryRegistry", SyntheticRegistry), patch(
            "app.routes.history_service_problem", return_value=None,
        ), patch("app.routes.backup_archive_problems", return_value=[]):
            asyncio.run(run())

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


class RuntimeOverrideOwnershipTests(ConfigReloadTestCase):
    @staticmethod
    def _field(payload: dict[str, Any], key: str) -> dict[str, Any]:
        return next(field for field in payload["fields"] if field["key"] == key)

    def _custom_path(self) -> Path:
        custom = self.root / "custom" / "timing.yaml"
        custom.parent.mkdir()
        custom.write_text("app:\n  smart_cache_ttl_seconds: 901\n", encoding="utf-8")
        self.config["paths"] = {"runtime_overrides_file": str(custom)}
        self._write_config()
        get_settings.cache_clear()
        return custom

    def test_custom_path_load_save_status_and_reload_agree(self) -> None:
        custom = self._custom_path()
        settings = get_settings()
        self.assertEqual(settings.app.smart_cache_ttl_seconds, 901)
        self.assertEqual(app_config.config_watch_paths(settings)[1], custom)
        response = app_config.save_runtime_behavior_overrides(settings, {"smart_cache_ttl_seconds": 902})
        for payload in (response, app_config.runtime_behavior_settings_payload()):
            self.assertEqual(payload["override_file"], str(custom))
            field = self._field(payload, "smart_cache_ttl_seconds")
            self.assertEqual(field["value"], 902)
            self.assertEqual(field["owner"], "admin")
            self.assertEqual(field["source"], "runtime-overrides.yaml")
            self.assertTrue(field["writable"])
        get_settings.cache_clear()
        self.assertEqual(get_settings().app.smart_cache_ttl_seconds, 902)
        self.assertEqual(yaml.safe_load(custom.read_text())["app"]["smart_cache_ttl_seconds"], 902)
        self.assertFalse(self.overrides_path.exists())
        self.reloader.prime()
        custom.write_text("app:\n  smart_cache_ttl_seconds: 903\n", encoding="utf-8")
        self.assertTrue(self._check())
        self.assertEqual(self.runtime.current().settings.app.smart_cache_ttl_seconds, 903)

    def test_pending_custom_path_uses_running_content_until_restart(self) -> None:
        custom = self._custom_path()
        self.reloader.prime()
        pending = self.root / "pending.yaml"
        pending.write_text("app:\n  smart_cache_ttl_seconds: 999\n", encoding="utf-8")
        self.overrides_path.write_text("app:\n  smart_cache_ttl_seconds: 401\n", encoding="utf-8")
        self.config["paths"]["runtime_overrides_file"] = str(pending)
        self._write_config()
        custom.write_text("app:\n  smart_cache_ttl_seconds: 903\n", encoding="utf-8")
        self.assertTrue(self._check())
        current = self.runtime.current().settings
        self.assertEqual(current.paths.runtime_overrides_file, str(custom))
        self.assertEqual(current.app.smart_cache_ttl_seconds, 903)
        self.assertEqual(self.reloader.restart_pending, ("paths",))
        self.assertEqual(app_config.config_watch_paths(current)[1], custom)
        self.assertEqual(self._field(app_config.runtime_behavior_settings_payload(current), "smart_cache_ttl_seconds")["value"], 903)
        self.assertEqual(app_config.load_settings().app.smart_cache_ttl_seconds, 999)
        self.assertEqual(app_config.load_settings().paths.runtime_overrides_file, str(pending))

    def test_pending_custom_path_is_validated_before_restart(self) -> None:
        from app.config_errors import ConfigurationError

        self._custom_path()
        running = get_settings()
        pending = self.root / "pending-invalid.yaml"
        pending.write_text("app:\n  smart_cache_ttl_seconds: invalid\n", encoding="utf-8")
        self.config["paths"]["runtime_overrides_file"] = str(pending)
        self._write_config()

        with self.assertRaises(ConfigurationError):
            app_config.load_settings(running_restart_only=running)

    def test_malformed_paths_report_configuration_error(self) -> None:
        from app.config_errors import ConfigurationError

        for paths in (None, [], {"runtime_overrides_file": []}):
            with self.subTest(paths=paths):
                self.config["paths"] = paths
                self._write_config()
                with self.assertRaises(ConfigurationError):
                    app_config.load_settings()

    def test_lock_key_survives_symlink_replacement(self) -> None:
        target = self.root / "target-overrides.yaml"
        target.write_text("{}\n", encoding="utf-8")
        link = self.root / "linked-overrides.yaml"
        link.symlink_to(target)
        before = app_config._runtime_override_lock_key(link)

        link.unlink()
        link.write_text("{}\n", encoding="utf-8")

        self.assertEqual(app_config._runtime_override_lock_key(link), before)

    def test_save_with_pending_path_returns_the_written_running_file(self) -> None:
        custom = self._custom_path()
        running = get_settings()
        pending = self.root / "pending.yaml"
        pending.write_text("app:\n  smart_cache_ttl_seconds: 999\n", encoding="utf-8")
        before = pending.read_bytes()
        self.config["paths"]["runtime_overrides_file"] = str(pending)
        self._write_config()
        response = app_config.save_runtime_behavior_overrides(running, {"smart_cache_ttl_seconds": 904})
        self.assertEqual(response["override_file"], str(custom))
        self.assertEqual(self._field(response, "smart_cache_ttl_seconds")["value"], 904)
        self.assertEqual(pending.read_bytes(), before)
        self.assertEqual(yaml.safe_load(custom.read_text())["app"]["smart_cache_ttl_seconds"], 904)
        self.assertEqual(get_settings().paths.runtime_overrides_file, str(custom))

    def test_custom_path_environment_ownership_and_legacy_precedence(self) -> None:
        custom = self._custom_path()
        custom.write_text("app:\n  smart_cache_ttl_seconds: 901\n  source_bundle_cache_ttl_seconds: 120\n", encoding="utf-8")
        before = custom.read_bytes()
        with patch.dict(os.environ, {"APP_SMART_CACHE_TTL_SECONDS": "777", "APP_CACHE_TTL": "15"}):
            get_settings.cache_clear()
            settings = get_settings()
            payload = app_config.runtime_behavior_settings_payload(settings)
            field = self._field(payload, "smart_cache_ttl_seconds")
            self.assertEqual(field["value"], 777)
            self.assertEqual(field["owner"], ".env")
            self.assertFalse(field["writable"])
            with self.assertRaisesRegex(ValueError, "owned by .env"):
                app_config.save_runtime_behavior_overrides(settings, {"smart_cache_ttl_seconds": 902})
            self.assertEqual(custom.read_bytes(), before)
            self.assertEqual(settings.app.source_bundle_cache_ttl_seconds, 120)
            self.assertEqual(settings.app.snapshot_cache_ttl_seconds, 15)
            self.assertEqual(self._field(payload, "source_bundle_cache_ttl_seconds")["owner"], "admin")

    def test_default_flat_and_legacy_layouts_still_load_overrides(self) -> None:
        for layout in ("flat", "config"):
            for explicit in (False, True):
                with self.subTest(layout=layout, legacy=explicit):
                    base = self.root / f"{layout}-{explicit}"
                    config_path = base / "config.yaml" if layout == "flat" else base / "config" / "config.yaml"
                    config_path.parent.mkdir(parents=True)
                    config_path.write_text(
                        "paths:\n  runtime_overrides_file: /app/config/runtime-overrides.yaml\n" if explicit else "{}\n",
                        encoding="utf-8",
                    )
                    override = config_path.with_name("runtime-overrides.yaml")
                    override.write_text("app:\n  smart_cache_ttl_seconds: 901\n", encoding="utf-8")
                    with patch.dict(os.environ, {"APP_CONFIG_PATH": str(config_path)}):
                        settings = app_config.load_settings()
                        self.assertEqual(settings.paths.runtime_overrides_file, str(override))
                        self.assertEqual(settings.app.smart_cache_ttl_seconds, 901)


class RuntimeOverrideTransactionTests(ConfigReloadTestCase):
    def test_partial_writers_serialize_reads_through_response(self) -> None:
        # Gate the first writer after its real read, then separately while it
        # builds its response. The second must not read in either interval.
        for phase in ("read", "response"):
            with self.subTest(phase=phase):
                self.overrides_path.write_text("app:\n  refresh_interval_seconds: 30\n", encoding="utf-8")
                settings = app_config.load_settings()
                entered = threading.Event()
                release = threading.Event()
                second_started = threading.Event()
                second_read = threading.Event()
                results: dict[str, Any] = {}
                errors: list[BaseException] = []
                real_load = app_config._load_runtime_overrides_config
                real_payload = app_config.runtime_behavior_settings_payload

                def load(path: Path) -> dict[str, Any]:
                    result = real_load(path)
                    if threading.current_thread().name == "second":
                        second_read.set()
                    if phase == "read" and threading.current_thread().name == "first" and not entered.is_set():
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError("first writer was not released")
                    return result

                def payload(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    if phase == "response" and threading.current_thread().name == "first":
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError("first response was not released")
                    return real_payload(*args, **kwargs)

                def writer(name: str, values: dict[str, int]) -> None:
                    try:
                        if name == "second":
                            second_started.set()
                        results[name] = app_config.save_runtime_behavior_overrides(settings, values)
                    except BaseException as exc:
                        errors.append(exc)

                first = threading.Thread(target=writer, name="first", args=("first", {"smart_cache_ttl_seconds": 777}))
                second = threading.Thread(target=writer, name="second", args=("second", {"refresh_interval_seconds": 55}))
                with patch.object(app_config, "_load_runtime_overrides_config", side_effect=load), patch.object(
                    app_config, "runtime_behavior_settings_payload", side_effect=payload,
                ):
                    first.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        second.start()
                        self.assertTrue(second_started.wait(5))
                        self.assertFalse(second_read.wait(0.2), "second writer read before the first transaction finished")
                    finally:
                        release.set()
                        first.join(5)
                        if second.ident is not None:
                            second.join(5)
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(second_read.is_set())
                self.assertEqual(yaml.safe_load(self.overrides_path.read_text())["app"], {
                    "refresh_interval_seconds": 55, "smart_cache_ttl_seconds": 777,
                })
                first_fields = {item["key"]: item["value"] for item in results["first"]["fields"]}
                second_fields = {item["key"]: item["value"] for item in results["second"]["fields"]}
                self.assertEqual((first_fields["smart_cache_ttl_seconds"], first_fields["refresh_interval_seconds"]), (777, 30))
                self.assertEqual((second_fields["smart_cache_ttl_seconds"], second_fields["refresh_interval_seconds"]), (777, 55))

    def test_each_writer_owns_a_unique_stage_and_leaves_other_files_alone(self) -> None:
        sibling = self.overrides_path.with_suffix(".tmp")
        sibling.write_text("another writer's stage", encoding="utf-8")
        stages: list[Path] = []
        real_replace = Path.replace

        def replace(source: Path, target: Path) -> Path:
            if Path(target) == self.overrides_path:
                stages.append(source)
                self.assertEqual(source.parent, self.overrides_path.parent)
            return real_replace(source, target)

        with patch.object(Path, "replace", replace):
            for value in (777, 778):
                app_config.save_runtime_behavior_overrides(get_settings(), {"smart_cache_ttl_seconds": value})
        self.assertTrue(sibling.exists(), "save consumed an unowned staging file")
        self.assertEqual(sibling.read_text(), "another writer's stage")
        self.assertEqual(len(set(stages)), 2)
        self.assertTrue(all(not stage.exists() for stage in stages))

    def test_failed_serialization_and_replace_clean_only_the_owned_stage(self) -> None:
        for failure in ("serialize", "replace"):
            with self.subTest(failure=failure):
                self.overrides_path.write_text("app:\n  smart_cache_ttl_seconds: 300\n", encoding="utf-8")
                settings = app_config.load_settings()
                before = self.overrides_path.read_bytes()
                sibling = self.overrides_path.with_suffix(".tmp")
                sibling.write_text("unowned", encoding="utf-8")
                inventory = set(self.overrides_path.parent.iterdir())

                def fail_dump(_data: Any, handle: Any, **_: Any) -> None:
                    handle.write("partial stage")
                    raise OSError("synthetic serialization failure")

                target = patch.object(yaml, "safe_dump", side_effect=fail_dump) if failure == "serialize" else patch.object(
                    Path, "replace", side_effect=OSError("synthetic replace failure"),
                )
                with target, self.assertRaises(OSError):
                    app_config.save_runtime_behavior_overrides(settings, {"smart_cache_ttl_seconds": 777})
                self.assertEqual(self.overrides_path.read_bytes(), before)
                self.assertEqual(sibling.read_text(), "unowned")
                self.assertEqual(set(self.overrides_path.parent.iterdir()), inventory)
                # A failed writer releases ownership; the next public save works.
                response = app_config.save_runtime_behavior_overrides(settings, {"smart_cache_ttl_seconds": 778})
                self.assertEqual(RuntimeOverrideOwnershipTests._field(response, "smart_cache_ttl_seconds")["value"], 778)


@unittest.skipUnless(os.name == "posix", "POSIX runtime-overrides reader permissions")
class RuntimeOverridePermissionTests(ConfigReloadTestCase):
    def test_new_file_keeps_stage_private_until_complete_then_publishes_readable(self) -> None:
        real_dump = yaml.safe_dump
        real_replace = Path.replace
        stages: list[Path] = []

        def dump(data: Any, handle: Any, **kwargs: Any) -> None:
            stages.append(Path(handle.name))
            self.assertEqual(stat.S_IMODE(os.fstat(handle.fileno()).st_mode), 0o600)
            self.assertFalse(self.overrides_path.exists())
            real_dump(data, handle, **kwargs)
            self.assertEqual(stat.S_IMODE(os.fstat(handle.fileno()).st_mode), 0o600)

        def replace(source: Path, target: Path) -> Path:
            if target == self.overrides_path:
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o644)
                self.assertEqual(yaml.safe_load(source.read_text())["app"]["smart_cache_ttl_seconds"], 777)
            return real_replace(source, target)

        # No process-wide umask change is needed in production. The publication
        # mode is explicit, even when the writer's inherited umask is private.
        previous = os.umask(0o077)
        try:
            with patch.object(yaml, "safe_dump", side_effect=dump), patch.object(Path, "replace", replace):
                response = app_config.save_runtime_behavior_overrides(get_settings(), {"smart_cache_ttl_seconds": 777})
        finally:
            os.umask(previous)
        metadata = self.overrides_path.stat()
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o644)
        self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertEqual(metadata.st_gid, os.getegid())
        self.assertEqual(len(stages), 1)
        self.assertFalse(stages[0].exists())
        self.assertEqual(RuntimeOverrideOwnershipTests._field(response, "smart_cache_ttl_seconds")["value"], 777)
        self.assertEqual(app_config.load_settings().app.smart_cache_ttl_seconds, 777)

    def test_existing_file_retains_owner_and_read_write_bits_without_special_bits(self) -> None:
        for mode in (0o644, 0o640, 0o660, 0o600, 0o400, 0o6755, 0o2770):
            with self.subTest(mode=oct(mode)):
                self.overrides_path.unlink(missing_ok=True)
                self.overrides_path.write_text("app:\n  smart_cache_ttl_seconds: 300\n")
                self.overrides_path.chmod(mode)
                before = self.overrides_path.stat()
                app_config.save_runtime_behavior_overrides(get_settings(), {"smart_cache_ttl_seconds": 777})
                after = self.overrides_path.stat()
                self.assertEqual(stat.S_IMODE(after.st_mode), mode & 0o666)
                self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
                self.assertEqual(app_config.load_settings().app.smart_cache_ttl_seconds, 777)

    def test_existing_shared_group_is_preserved_on_real_replacement(self) -> None:
        groups = sorted(set(os.getgroups()) - {os.getegid()})
        if not groups:
            self.skipTest("no supplementary group available for owned-fixture chgrp")
        self.overrides_path.write_text("app:\n  smart_cache_ttl_seconds: 300\n")
        # Only this test-owned file changes group, and only to an existing
        # membership. No root, setuid, or changes to the process identity.
        os.chown(self.overrides_path, -1, groups[0])
        self.overrides_path.chmod(0o640)
        before = self.overrides_path.stat()
        with patch.object(os, "fchown", wraps=os.fchown) as chown:
            app_config.save_runtime_behavior_overrides(get_settings(), {"smart_cache_ttl_seconds": 777})
        self.assertEqual(chown.call_count, 1)
        self.assertEqual(chown.call_args.args[1:], (before.st_uid, before.st_gid))
        after = self.overrides_path.stat()
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o640)

    def test_new_file_inherits_shared_group_from_setgid_parent(self) -> None:
        groups = sorted(set(os.getgroups()) - {os.getegid()})
        if not groups:
            self.skipTest("no supplementary group available for owned-fixture chgrp")
        parent = self.overrides_path.parent
        os.chown(parent, -1, groups[0])
        parent.chmod(0o2770)
        before = parent.stat()
        app_config.save_runtime_behavior_overrides(get_settings(), {"smart_cache_ttl_seconds": 777})
        metadata = self.overrides_path.stat()
        self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertEqual(metadata.st_gid, groups[0])
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o644)
        after = parent.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode), (before.st_uid, before.st_gid, before.st_mode))

    def test_declared_reader_identities_preserve_existing_owner_before_mode(self) -> None:
        services = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.nonroot.yml").read_text())["services"]
        self.assertEqual(services["enclosure-admin"]["user"], "0:${APP_GID:-10001}")
        self.assertEqual(services["enclosure-ui"]["user"], "${APP_UID:-10001}:${APP_GID:-10001}")
        # UID ownership transfer is modeled, not a live different-UID open.
        # Real modes, descriptor, serialization and replace still execute.
        for uid, mode in ((0, 0o640), (10001, 0o600)):
            with self.subTest(uid=uid, mode=oct(mode)):
                self.overrides_path.write_text("app:\n  smart_cache_ttl_seconds: 300\n")
                self.overrides_path.chmod(mode)
                real_stat = Path.stat
                real_chmod = os.fchmod
                events: list[str] = []

                def target_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
                    metadata = real_stat(path, *args, **kwargs)
                    if path == self.overrides_path:
                        fields = list(metadata)
                        fields[4:6] = [uid, 10001]
                        return os.stat_result(fields)
                    return metadata

                def chown(fd: int, owner: int, group: int) -> None:
                    self.assertEqual((owner, group), (uid, 10001))
                    self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
                    events.append("owner")

                def chmod(fd: int, published_mode: int) -> None:
                    self.assertEqual(events, ["owner"])
                    self.assertEqual(published_mode, mode)
                    events.append("mode")
                    real_chmod(fd, published_mode)

                with patch.object(Path, "stat", target_stat), patch.object(os, "fchown", side_effect=chown), patch.object(
                    os, "fchmod", side_effect=chmod,
                ):
                    app_config.save_runtime_behavior_overrides(get_settings(), {"smart_cache_ttl_seconds": 777})
                self.assertEqual(events, ["owner", "mode"])

    def test_metadata_failure_preserves_canonical_and_unowned_files_then_recovers(self) -> None:
        for phase in ("stat", "owner", "mode"):
            with self.subTest(phase=phase):
                self.overrides_path.write_text("app:\n  smart_cache_ttl_seconds: 300\n")
                self.overrides_path.chmod(0o640)
                settings = app_config.load_settings()
                sibling = self.overrides_path.with_suffix(".tmp")
                sibling.write_text("unowned")
                paths = list(self.overrides_path.parent.iterdir())
                before = {path: (path.read_bytes(), path.stat()) for path in paths}
                real_stat = Path.stat
                real_fstat = os.fstat

                def target_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
                    if path == self.overrides_path:
                        raise PermissionError("synthetic metadata refusal")
                    return real_stat(path, *args, **kwargs)

                def stage_stat(fd: int) -> os.stat_result:
                    fields = list(real_fstat(fd))
                    fields[4] += 1  # force ownership restoration without host chown
                    return os.stat_result(fields)

                target = patch.object(Path, "stat", target_stat) if phase == "stat" else patch.object(
                    os, "fchown" if phase == "owner" else "fchmod", side_effect=PermissionError("synthetic metadata refusal"),
                )
                with target, patch.object(os, "fstat", side_effect=stage_stat if phase == "owner" else real_fstat), patch.object(
                    Path, "replace", autospec=True, side_effect=Path.replace,
                ) as replace:
                    with self.assertRaises(PermissionError):
                        app_config.save_runtime_behavior_overrides(settings, {"smart_cache_ttl_seconds": 777})
                    replace.assert_not_called()
                self.assertEqual(set(self.overrides_path.parent.iterdir()), set(paths))
                for path, (data, metadata) in before.items():
                    self.assertEqual(path.read_bytes(), data)
                    self.assertEqual(path.stat(), metadata)
                response = app_config.save_runtime_behavior_overrides(settings, {"smart_cache_ttl_seconds": 778})
                self.assertEqual(RuntimeOverrideOwnershipTests._field(response, "smart_cache_ttl_seconds")["value"], 778)

    def test_new_file_permission_failure_cleans_stage_without_publication(self) -> None:
        sibling = self.overrides_path.with_suffix(".tmp")
        sibling.write_text("unowned")
        before = set(self.overrides_path.parent.iterdir())
        with patch.object(os, "fchmod", side_effect=PermissionError("synthetic mode refusal")):
            with self.assertRaises(PermissionError):
                app_config.save_runtime_behavior_overrides(get_settings(), {"smart_cache_ttl_seconds": 777})
        self.assertFalse(self.overrides_path.exists())
        self.assertEqual(set(self.overrides_path.parent.iterdir()), before)
        self.assertEqual(sibling.read_text(), "unowned")


class ModuleCacheCompatibilityTests(unittest.TestCase):
    def test_get_settings_cache_clear_still_forces_a_fresh_load(self) -> None:
        first = app_config.get_settings()
        self.assertIs(app_config.get_settings(), first)
        app_config.get_settings.cache_clear()
        self.assertIsNot(app_config.get_settings(), first)
        app_config.get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
