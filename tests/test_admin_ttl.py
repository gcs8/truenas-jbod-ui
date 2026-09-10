from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

# Must precede admin_service.main, which builds its app at import time.
from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN
from admin_service.config import AdminSettings, get_admin_settings
from admin_service.main import compute_expires_at, create_app, prepare_admin_directories
from app.config_errors import ConfigurationError


class _ReleaseStatusStub:
    async def run_periodic_refresh(self) -> None:
        await asyncio.Event().wait()


class AdminAutoStopContractTests(unittest.TestCase):
    def test_application_default_disables_auto_stop(self) -> None:
        settings = AdminSettings()

        self.assertEqual(settings.auto_stop_seconds, 0)
        self.assertIsNone(compute_expires_at(settings))

    def test_negative_auto_stop_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AdminSettings(auto_stop_seconds=-1)

    def test_environment_auto_stop_accepts_only_integer_strings(self) -> None:
        for raw_value, expected in (("0", 0), ("17", 17)):
            with self.subTest(raw_value=raw_value):
                get_admin_settings.cache_clear()
                with patch.dict("os.environ", {"ADMIN_AUTO_STOP_SECONDS": raw_value}, clear=True):
                    self.assertEqual(get_admin_settings().auto_stop_seconds, expected)

        for raw_value in ("true", "false", "1.0", "1e3", "seventeen"):
            with self.subTest(raw_value=raw_value):
                get_admin_settings.cache_clear()
                with (
                    patch.dict("os.environ", {"ADMIN_AUTO_STOP_SECONDS": raw_value}, clear=True),
                    self.assertRaises(ConfigurationError) as captured,
                ):
                    get_admin_settings()
                self.assertEqual(
                    str(captured.exception),
                    "Configuration error: ADMIN_AUTO_STOP_SECONDS in .env must be a whole number "
                    "of seconds (0 disables auto-stop).",
                )

        get_admin_settings.cache_clear()
        with patch.dict("os.environ", {"ADMIN_AUTO_STOP_SECONDS": ""}, clear=True):
            self.assertEqual(get_admin_settings().auto_stop_seconds, 0)
        get_admin_settings.cache_clear()

    def test_positive_auto_stop_keeps_expiry_and_shutdown_task(self) -> None:
        settings = AdminSettings(auto_stop_seconds=17, public_origin=ADMIN_TEST_PUBLIC_ORIGIN)
        self.assertIsNotNone(compute_expires_at(settings))

        async def exercise() -> list[int]:
            calls: list[int] = []

            async def fake_shutdown(seconds: int) -> None:
                calls.append(seconds)
                await asyncio.Event().wait()

            with (
                patch("admin_service.main.get_admin_settings", return_value=settings),
                patch("admin_service.main.get_release_status_service", return_value=_ReleaseStatusStub()),
                patch("admin_service.main._shutdown_after_ttl", side_effect=fake_shutdown),
            ):
                app = create_app()
                async with app.router.lifespan_context(app):
                    await asyncio.sleep(0)
            return calls

        self.assertEqual(asyncio.run(exercise()), [17])

    def test_disabled_auto_stop_creates_no_shutdown_task(self) -> None:
        settings = AdminSettings(auto_stop_seconds=0, public_origin=ADMIN_TEST_PUBLIC_ORIGIN)

        async def exercise() -> list[int]:
            calls: list[int] = []

            async def fake_shutdown(seconds: int) -> None:
                calls.append(seconds)

            with (
                patch("admin_service.main.get_admin_settings", return_value=settings),
                patch("admin_service.main.get_release_status_service", return_value=_ReleaseStatusStub()),
                patch("admin_service.main._shutdown_after_ttl", side_effect=fake_shutdown),
            ):
                app = create_app()
                async with app.router.lifespan_context(app):
                    await asyncio.sleep(0)
            return calls

        self.assertEqual(asyncio.run(exercise()), [])


class AdminDirectoryPreparationTests(unittest.TestCase):
    def test_writable_host_prep_folder_is_created_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "host-prep"
            prepare_admin_directories(AdminSettings(host_prep_temp_dir=str(target)))
            self.assertTrue(target.is_dir())

    def test_unwritable_host_prep_folder_stops_with_a_named_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blocker = Path(temp_dir) / "blocker.txt"
            blocker.write_text("not a folder\n", encoding="utf-8")
            target = blocker / "host-prep"
            with (
                self.assertLogs("admin_service.main", level="ERROR") as logs,
                self.assertRaises(RuntimeError) as captured,
            ):
                prepare_admin_directories(AdminSettings(host_prep_temp_dir=str(target)))

        message = str(captured.exception)
        self.assertIn(f"Admin cannot create the folder {target}", message)
        self.assertIn("check ADMIN_HOST_PREP_TEMP_DIR and the admin container mounts", message)
        self.assertEqual(logs.output, [f"ERROR:admin_service.main:Configuration error: {message}"])
