from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from admin_service.config import AdminSettings
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
