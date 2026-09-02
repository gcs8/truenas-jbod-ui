from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import main as app_main
from app.config import Settings
from app.models.domain import InventorySnapshot


def _route(path: str, method: str):
    return next(
        route
        for route in app_main.app.routes
        if getattr(route, "path", "") == path and method in (getattr(route, "methods", None) or set())
    )


def _service(layout_slot_count: int | None) -> Mock:
    service = Mock()
    service.system.id = "system-a"
    service.system.truenas.platform = "scale"
    if layout_slot_count is None:
        service.get_snapshot = AsyncMock(side_effect=RuntimeError("collector down"))
    else:
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(
                slots=[],
                layout_slot_count=layout_slot_count,
                refresh_interval_seconds=30,
            )
        )
    service.get_slot_smart_summary = AsyncMock(return_value={"slot": 78})
    return service


class SlotBoundsFollowSelectedEnclosureTests(unittest.TestCase):
    """Regression for #168 / #213: an 84-bay MD1280 under the default
    ``LAYOUT_SLOT_COUNT=60`` rendered bays 60-83 but every slot route 404'd
    for them, while a 12-bay shelf on the same system accepted slot 40."""

    def setUp(self) -> None:
        self.settings = Settings()
        self.assertEqual(self.settings.layout.slot_count, 60)

    def test_bay_above_global_layout_is_accepted_when_profile_has_it(self) -> None:
        service = _service(layout_slot_count=84)
        asyncio.run(app_main.ensure_slot_bounds(self.settings, 78, service, "50050cc11ac013fc"))
        service.get_snapshot.assert_awaited_once_with(
            selected_enclosure_id="50050cc11ac013fc",
            allow_stale_cache=True,
        )

    def test_bay_beyond_selected_profile_is_rejected_even_below_global_layout(self) -> None:
        service = _service(layout_slot_count=12)
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_main.ensure_slot_bounds(self.settings, 40, service, "small-shelf"))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("Slot 40", raised.exception.detail)

    def test_negative_slot_never_touches_the_service(self) -> None:
        service = _service(layout_slot_count=84)
        with self.assertRaises(HTTPException):
            asyncio.run(app_main.ensure_slot_bounds(self.settings, -1, service, "50050cc11ac013fc"))
        service.get_snapshot.assert_not_awaited()

    def test_global_layout_is_the_fallback_when_no_snapshot_is_available(self) -> None:
        for service in (_service(layout_slot_count=None), _service(layout_slot_count=0), None):
            with self.subTest(service=service):
                asyncio.run(app_main.ensure_slot_bounds(self.settings, 59, service, "enc"))
                with self.assertRaises(HTTPException):
                    asyncio.run(app_main.ensure_slot_bounds(self.settings, 60, service, "enc"))

    def test_smart_route_reaches_the_service_for_an_md1280_upper_bay(self) -> None:
        route = _route("/api/slots/{slot}/smart", "GET")
        service = _service(layout_slot_count=84)
        registry = Mock()
        registry.get_service.return_value = service

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_settings", return_value=self.settings),
            patch.object(app_main, "add_perf_metadata"),
        ):
            asyncio.run(
                route.endpoint(
                    slot=78,
                    system_id="system-a",
                    enclosure_id="50050cc11ac013fc",
                )
            )
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    route.endpoint(
                        slot=84,
                        system_id="system-a",
                        enclosure_id="50050cc11ac013fc",
                    )
                )

        service.get_slot_smart_summary.assert_awaited_once_with(
            78,
            selected_enclosure_id="50050cc11ac013fc",
            allow_stale_cache=True,
        )
        self.assertEqual(raised.exception.status_code, 404)

    def test_smart_batch_resolves_the_layout_once_for_all_slots(self) -> None:
        route = _route("/api/slots/smart-batch", "POST")
        service = _service(layout_slot_count=84)
        service.get_slot_smart_summaries = AsyncMock(return_value=[])
        registry = Mock()
        registry.get_service.return_value = service
        payload = Mock()
        payload.slots = [5, 63, 83]
        payload.max_concurrency = 2

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_settings", return_value=self.settings),
            patch.object(app_main, "add_perf_metadata"),
        ):
            try:
                asyncio.run(route.endpoint(payload=payload, system_id="system-a", enclosure_id="50050cc11ac013fc"))
            except HTTPException as exc:  # pragma: no cover - the bounds check must not fire
                self.fail(f"bounds check rejected a profile bay: {exc.detail}")
            except Exception:
                # Downstream service wiring is mocked loosely; only the bounds
                # behaviour is under test here.
                pass
            payload.slots = [5, 84]
            with self.assertRaises(HTTPException):
                asyncio.run(route.endpoint(payload=payload, system_id="system-a", enclosure_id="50050cc11ac013fc"))

        self.assertEqual(service.get_snapshot.await_count, 2)


if __name__ == "__main__":
    unittest.main()
