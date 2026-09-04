from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from fastapi import HTTPException

from app import main as app_main
from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView, SmartSummaryView, utcnow
from app.services.inventory import InventoryService
from app.services.profile_registry import dell_md1280_bottom_drawer_slot_layout
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore


def _route(path: str, method: str):
    return next(
        route
        for route in app_main.app.routes
        if getattr(route, "path", "") == path and method in (getattr(route, "methods", None) or set())
    )


def _service(
    layout_slot_count: int | None,
    selected_enclosure_id: str | None = "50050cc11ac013fc",
    slots: list[SlotView] | None = None,
    layout_rows: list[list[int | None]] | None = None,
) -> Mock:
    service = Mock()
    service.system.id = "system-a"
    service.system.truenas.platform = "scale"
    if layout_slot_count is None:
        service.get_snapshot = AsyncMock(side_effect=RuntimeError("collector down"))
    else:
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(
                slots=slots or [],
                layout_rows=layout_rows or [],
                layout_slot_count=layout_slot_count,
                selected_enclosure_id=selected_enclosure_id,
                refresh_interval_seconds=30,
            )
        )
    service.get_slot_smart_summary = AsyncMock(return_value={"slot": 78})
    return service


def _service_with_cached_smart(
    summaries: dict[int, SmartSummaryView],
) -> tuple[InventoryService, AsyncMock]:
    service = object.__new__(InventoryService)
    service.settings = Settings()
    service.system = SystemConfig(
        id="system-a",
        truenas=TrueNASConfig(platform="scale"),
    )
    service.slot_detail_store = None
    service._smart_cache = {}
    service._smart_cache_until = {}
    service._smart_cache_global_generation = 0
    service._smart_cache_enclosure_generations = {}
    service._observe_inventory_cache_metrics = Mock()
    service._observe_smart_summary_request = Mock()
    for slot, summary in summaries.items():
        key = ("system-a", "scale", "enc-a", slot, (f"/dev/sd{slot}",))
        service._smart_cache[key] = summary
        service._smart_cache_until[key] = utcnow() + timedelta(minutes=5)
    snapshot_lookup = AsyncMock(side_effect=RuntimeError("collector down"))
    setattr(service, "get_snapshot", snapshot_lookup)
    return service, snapshot_lookup


BOTTOM_DRAWER_ID = "50050cc11ac013fc::dell-md1280-drawer-bottom-42"


def _layout_slots(layout_rows: list[list[int | None]]) -> list[int]:
    return sorted(slot for row in layout_rows for slot in row if slot is not None)


def _drawer_service(layout_rows_in_snapshot: bool = False) -> Mock:
    """Snapshot shaped like the MD1280 bottom-drawer sub-view: the visible bays
    are numbered 42-83 while ``layout_slot_count`` reports the 42 visible bays,
    exactly as ``_correlate_scale_linux`` renders a drawer sub-view."""
    layout_rows = dell_md1280_bottom_drawer_slot_layout()
    rendered = _layout_slots(layout_rows)
    slot_views = [
        SlotView(slot=slot, slot_label=str(slot + 1), row_index=0, column_index=index)
        for index, slot in enumerate(rendered)
    ]
    return _service(
        layout_slot_count=len(rendered),
        selected_enclosure_id=BOTTOM_DRAWER_ID,
        slots=[] if layout_rows_in_snapshot else slot_views,
        layout_rows=layout_rows if layout_rows_in_snapshot else None,
    )


class SlotBoundsFollowSelectedEnclosureTests(unittest.TestCase):
    """Regression for #168 / #213: an 84-bay MD1280 under the default
    ``LAYOUT_SLOT_COUNT=60`` rendered bays 60-83 but every slot route 404'd
    for them, while a 12-bay shelf on the same system accepted slot 40."""

    def setUp(self) -> None:
        self.settings = Settings()
        self.assertEqual(self.settings.layout.slot_count, 60)

    def test_bay_above_global_layout_is_accepted_when_profile_has_it(self) -> None:
        service = _service(layout_slot_count=84)
        asyncio.run(app_main.ensure_slot_bounds(78, service, "50050cc11ac013fc"))
        service.get_snapshot.assert_awaited_once_with(
            selected_enclosure_id="50050cc11ac013fc",
            allow_stale_cache=True,
        )

    def test_bay_beyond_selected_profile_is_rejected_even_below_global_layout(self) -> None:
        service = _service(layout_slot_count=12, selected_enclosure_id="small-shelf")
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_main.ensure_slot_bounds(40, service, "small-shelf"))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("Slot 40", raised.exception.detail)

    def test_negative_slot_never_touches_the_service(self) -> None:
        service = _service(layout_slot_count=84)
        with self.assertRaises(HTTPException):
            asyncio.run(app_main.ensure_slot_bounds(-1, service, "50050cc11ac013fc"))
        service.get_snapshot.assert_not_awaited()

    def test_slot_bounds_require_an_inventory_service(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_main.ensure_slot_bounds(59, None, "enc"))
        self.assertEqual(raised.exception.status_code, 503)

    def test_scoped_bounds_fail_closed_when_the_snapshot_is_unavailable(self) -> None:
        service = _service(layout_slot_count=None)

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_main.ensure_slot_bounds(40, service, "small-shelf"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Unable to resolve selected enclosure layout.")

    def test_scoped_bounds_reject_a_snapshot_for_a_different_enclosure(self) -> None:
        service = _service(layout_slot_count=84)
        service.get_snapshot.return_value.selected_enclosure_id = "other-shelf"

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_main.ensure_slot_bounds(40, service, "small-shelf"))

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "Enclosure 'small-shelf' is not available for this system.")

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


class SlotBoundsFollowRenderedSlotsTests(unittest.TestCase):
    """Regression for #275: a drawer sub-view numbers its bays from a non-zero
    base (MD1280 bottom drawer = bays 42-83) while ``layout_slot_count`` only
    says how many bays are visible, so a count-based bound rejected every real
    bay in the drawer and accepted the other drawer's bays instead."""

    def test_drawer_sub_view_admits_every_rendered_bay(self) -> None:
        service = _drawer_service()
        for slot in range(42, 84):
            with self.subTest(slot=slot):
                asyncio.run(app_main.ensure_slot_bounds(slot, service, BOTTOM_DRAWER_ID))

    def test_drawer_sub_view_rejects_bays_from_the_other_drawer(self) -> None:
        service = _drawer_service()
        for slot in (0, 41, 84):
            with self.subTest(slot=slot), self.assertRaises(HTTPException) as raised:
                asyncio.run(app_main.ensure_slot_bounds(slot, service, BOTTOM_DRAWER_ID))
            self.assertEqual(raised.exception.status_code, 404)
            self.assertIn(f"Slot {slot}", raised.exception.detail)

    def test_layout_rows_bound_the_view_when_no_slot_views_are_rendered_yet(self) -> None:
        service = _drawer_service(layout_rows_in_snapshot=True)
        asyncio.run(app_main.ensure_slot_bounds(42, service, BOTTOM_DRAWER_ID))
        asyncio.run(app_main.ensure_slot_bounds(83, service, BOTTOM_DRAWER_ID))
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_main.ensure_slot_bounds(0, service, BOTTOM_DRAWER_ID))
        self.assertEqual(raised.exception.status_code, 404)

    def test_noncontiguous_layout_rejects_ids_in_the_gap(self) -> None:
        service = _service(
            layout_slot_count=6,
            selected_enclosure_id="sparse-shelf",
            layout_rows=[[0, 1, 2], [10, 11, 12]],
        )
        asyncio.run(app_main.ensure_slot_bounds(2, service, "sparse-shelf"))
        asyncio.run(app_main.ensure_slot_bounds(10, service, "sparse-shelf"))
        for slot in (5, 6, 13):
            with self.subTest(slot=slot), self.assertRaises(HTTPException) as raised:
                asyncio.run(app_main.ensure_slot_bounds(slot, service, "sparse-shelf"))
            self.assertEqual(raised.exception.status_code, 404)

    def test_count_only_snapshot_keeps_the_zero_based_rule(self) -> None:
        service = _service(layout_slot_count=12, selected_enclosure_id="small-shelf")
        asyncio.run(app_main.ensure_slot_bounds(11, service, "small-shelf"))
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_main.ensure_slot_bounds(12, service, "small-shelf"))
        self.assertEqual(raised.exception.status_code, 404)

    def test_smart_batch_admits_drawer_bays_and_resolves_the_layout_once(self) -> None:
        route = _route("/api/slots/smart-batch", "POST")
        service = _drawer_service()
        service.get_slot_smart_summaries = AsyncMock(return_value=[])
        registry = Mock()
        registry.get_service.return_value = service
        payload = Mock()
        payload.slots = [42, 57, 83]
        payload.max_concurrency = 2

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_settings", return_value=Settings()),
            patch.object(app_main, "add_perf_metadata"),
        ):
            try:
                asyncio.run(route.endpoint(payload=payload, system_id="system-a", enclosure_id=BOTTOM_DRAWER_ID))
            except HTTPException as exc:  # pragma: no cover - the bounds check must not fire
                self.fail(f"bounds check rejected a drawer bay: {exc.detail}")
            except Exception:
                # Downstream service wiring is mocked loosely; only the bounds
                # behaviour is under test here.
                pass
            payload.slots = [42, 5]
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(route.endpoint(payload=payload, system_id="system-a", enclosure_id=BOTTOM_DRAWER_ID))

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("Slot 5", raised.exception.detail)
        self.assertEqual(service.get_snapshot.await_count, 2)

    def test_history_scope_admits_drawer_bays_and_rejects_the_other_drawer(self) -> None:
        route = _route("/api/history/scope", "GET")
        service = _drawer_service()
        registry = Mock()
        registry.get_service.return_value = service
        history_backend = Mock()
        history_backend.configured = True
        history_backend.get_scope_history = AsyncMock(return_value={})

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend),
            patch.object(app_main, "add_perf_metadata"),
        ):
            response = asyncio.run(
                route.endpoint(
                    system_id="system-a",
                    enclosure_id=BOTTOM_DRAWER_ID,
                    slots=[42, 83],
                    window_hours=24,
                    metrics=None,
                    event_limit=12,
                )
            )
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    route.endpoint(
                        system_id="system-a",
                        enclosure_id=BOTTOM_DRAWER_ID,
                        slots=[41],
                        window_hours=24,
                        metrics=None,
                        event_limit=12,
                    )
                )

        self.assertEqual(response.status_code, 200)
        history_backend.get_scope_history.assert_awaited_once()
        self.assertEqual(history_backend.get_scope_history.await_args.kwargs["slots"], [42, 83])
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("Slot 41", raised.exception.detail)
        self.assertEqual(service.get_snapshot.await_count, 2)


class DegradedReadSlotBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _service(layout_slot_count=None)
        self.registry = Mock()
        self.registry.get_service.return_value = self.service

    def test_slot_history_uses_sidecar_when_layout_is_unavailable(self) -> None:
        route = _route("/api/slots/{slot}/history", "GET")
        history_backend = Mock()
        history_backend.get_slot_history = AsyncMock(
            return_value={"configured": True, "available": True, "slot": 5, "metrics": {}}
        )

        with (
            patch.object(app_main, "get_inventory_registry", return_value=self.registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend),
            patch.object(app_main, "add_perf_metadata"),
        ):
            response = asyncio.run(
                route.endpoint(slot=5, system_id="system-a", enclosure_id="enc-a", window_hours=24)
            )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["layout_bounds"], "unavailable")
        history_backend.get_slot_history.assert_awaited_once_with(
            5,
            "system-a",
            "enc-a",
            window_hours=24,
        )

    def test_history_scope_uses_sidecar_when_layout_is_unavailable(self) -> None:
        route = _route("/api/history/scope", "GET")
        history_backend = Mock()
        history_backend.configured = True
        history_backend.get_scope_history = AsyncMock(return_value={5: {"metrics": {}}})

        with (
            patch.object(app_main, "get_inventory_registry", return_value=self.registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend),
            patch.object(app_main, "add_perf_metadata"),
        ):
            response = asyncio.run(
                route.endpoint(
                    system_id="system-a",
                    enclosure_id="enc-a",
                    slots=[5],
                    window_hours=24,
                    metrics=None,
                    event_limit=12,
                )
            )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["layout_bounds"], "unavailable")
        self.assertEqual(payload["histories"], {"5": {"metrics": {}}})

    def test_cached_smart_read_continues_when_layout_is_unavailable(self) -> None:
        route = _route("/api/slots/{slot}/smart", "GET")
        self.service.get_cached_slot_smart_summary_without_layout = Mock(
            return_value=SmartSummaryView(available=True)
        )

        with (
            patch.object(app_main, "get_inventory_registry", return_value=self.registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            summary = asyncio.run(
                route.endpoint(slot=5, system_id="system-a", enclosure_id="enc-a", fresh=False)
            )

        self.assertTrue(summary.available)
        self.assertEqual(summary.layout_bounds, "unavailable")
        self.service.get_cached_slot_smart_summary_without_layout.assert_called_once_with(
            5,
            selected_enclosure_id="enc-a",
        )
        self.service.get_slot_smart_summary.assert_not_awaited()

    def test_cached_smart_read_bypasses_a_second_snapshot_lookup(self) -> None:
        route = _route("/api/slots/{slot}/smart", "GET")
        service, snapshot_lookup = _service_with_cached_smart(
            {5: SmartSummaryView(available=True, temperature_c=31)}
        )
        registry = Mock()
        registry.get_service.return_value = service

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            summary = asyncio.run(
                route.endpoint(slot=5, system_id="system-a", enclosure_id="enc-a", fresh=False)
            )

        self.assertEqual(summary.temperature_c, 31)
        self.assertEqual(summary.layout_bounds, "unavailable")
        self.assertEqual(snapshot_lookup.await_count, 1)

    def test_layout_unavailable_smart_read_uses_persisted_last_good_data(self) -> None:
        service, _snapshot_lookup = _service_with_cached_smart({})
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SlotDetailStore(str(Path(temp_dir) / "slot-details.json"))
            store.save_entries(
                [
                    SlotDetailCacheEntry(
                        system_id="system-a",
                        enclosure_id="enc-a",
                        slot=5,
                        identifiers=["serial-a"],
                        smart_fields={"available": True, "temperature_c": 33},
                    )
                ]
            )
            service.slot_detail_store = store

            summary = service.get_cached_slot_smart_summary_without_layout(5, "enc-a")

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertTrue(summary.available)
        self.assertEqual(summary.temperature_c, 33)

    def test_fresh_smart_read_keeps_strict_layout_bounds(self) -> None:
        route = _route("/api/slots/{slot}/smart", "GET")
        self.service.get_slot_smart_summary = AsyncMock(return_value=SmartSummaryView(available=True))

        with (
            patch.object(app_main, "get_inventory_registry", return_value=self.registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    route.endpoint(slot=5, system_id="system-a", enclosure_id="enc-a", fresh=True)
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.service.get_slot_smart_summary.assert_not_awaited()

    def test_cached_smart_batch_continues_when_layout_is_unavailable(self) -> None:
        route = _route("/api/slots/smart-batch", "POST")
        self.service.get_cached_slot_smart_summary_without_layout = Mock(return_value=None)
        self.service.get_slot_smart_summaries = AsyncMock(return_value=[])
        payload = Mock(slots=[5, 6], max_concurrency=2)

        with (
            patch.object(app_main, "get_inventory_registry", return_value=self.registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            response = asyncio.run(
                route.endpoint(
                    payload=payload,
                    system_id="system-a",
                    enclosure_id="enc-a",
                    fresh=False,
                )
            )

        self.assertEqual(response.layout_bounds, "unavailable")
        self.assertEqual([item.slot for item in response.summaries], [5, 6])
        self.assertTrue(all(not item.summary.available for item in response.summaries))
        self.assertEqual(
            self.service.get_cached_slot_smart_summary_without_layout.call_args_list,
            [
                call(5, selected_enclosure_id="enc-a"),
                call(6, selected_enclosure_id="enc-a"),
            ],
        )
        self.service.get_slot_smart_summaries.assert_not_awaited()

    def test_cached_smart_batch_bypasses_a_second_snapshot_lookup(self) -> None:
        route = _route("/api/slots/smart-batch", "POST")
        service, snapshot_lookup = _service_with_cached_smart(
            {
                5: SmartSummaryView(available=True, temperature_c=31),
                6: SmartSummaryView(available=True, temperature_c=32),
            }
        )
        registry = Mock()
        registry.get_service.return_value = service
        payload = Mock(slots=[5, 6], max_concurrency=2)

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            response = asyncio.run(
                route.endpoint(
                    payload=payload,
                    system_id="system-a",
                    enclosure_id="enc-a",
                    fresh=False,
                )
            )

        self.assertEqual(
            [item.summary.temperature_c for item in response.summaries],
            [31, 32],
        )
        self.assertEqual(response.layout_bounds, "unavailable")
        self.assertEqual(snapshot_lookup.await_count, 1)

    def test_fresh_smart_batch_keeps_strict_layout_bounds(self) -> None:
        route = _route("/api/slots/smart-batch", "POST")
        self.service.get_slot_smart_summaries = AsyncMock(return_value=[])
        payload = Mock(slots=[5, 6], max_concurrency=2)

        with (
            patch.object(app_main, "get_inventory_registry", return_value=self.registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    route.endpoint(
                        payload=payload,
                        system_id="system-a",
                        enclosure_id="enc-a",
                        fresh=True,
                    )
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.service.get_slot_smart_summaries.assert_not_awaited()

    def test_mutation_keeps_strict_layout_bounds_when_layout_is_unavailable(self) -> None:
        route = _route("/api/slots/{slot}/led", "POST")
        self.service.set_slot_led = AsyncMock()
        payload = Mock(action="on")

        with (
            patch.object(app_main, "get_inventory_registry", return_value=self.registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    route.endpoint(slot=5, payload=payload, system_id="system-a", enclosure_id="enc-a")
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.service.set_slot_led.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
