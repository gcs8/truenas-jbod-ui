from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from admin_service.config import AdminSettings, get_admin_settings
from admin_service.main import compute_expires_at, create_app


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

        for raw_value in ("true", "false", "1.0", "1e3", "", "seventeen"):
            with self.subTest(raw_value=raw_value):
                get_admin_settings.cache_clear()
                with (
                    patch.dict("os.environ", {"ADMIN_AUTO_STOP_SECONDS": raw_value}, clear=True),
                    self.assertRaises(ValidationError),
                ):
                    get_admin_settings()
        get_admin_settings.cache_clear()

    def test_positive_auto_stop_keeps_expiry_and_shutdown_task(self) -> None:
        settings = AdminSettings(auto_stop_seconds=17)
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
        settings = AdminSettings(auto_stop_seconds=0)

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
