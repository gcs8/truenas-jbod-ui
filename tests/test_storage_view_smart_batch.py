"""Storage-view SMART batching (#457).

A storage-view scope used to collect SMART one HTTP request per slot, so a
60-slot view cost 60 sequential requests per history pass. The main UI now has
POST /api/storage-views/{view_id}/slots/smart-batch and the collector asks for
a view in chunks of HISTORY_SMART_BATCH_SIZE, like enclosure scopes.
"""

from __future__ import annotations

import asyncio
import errno
import json
import unittest
from collections import OrderedDict
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import main as app_main
from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import (
    SmartBatchItem,
    SmartSummaryView,
    StorageViewRuntimePayload,
    StorageViewRuntimeSlot,
    StorageViewRuntimeView,
)
from app.services.inventory import InventoryService
from app.services.truenas_ws import TrueNASAPIError
from history_service.collector import HistoryCollector, ScopeSnapshot
from history_service.config import HistorySettings
from history_service.diagnostics import HistorySourceError

VIEW_ID = "boot-doms"
BATCH_PATH = f"/api/storage-views/{VIEW_ID}/slots/smart-batch"


def _route(path: str, method: str):
    return next(
        route
        for route in app_main.app.routes
        if getattr(route, "path", "") == path and method in (getattr(route, "methods", None) or set())
    )


def _runtime(slot_count: int, *, live_every: int = 2) -> StorageViewRuntimePayload:
    """A view whose even slots sit on live enclosure bays and odd slots do not."""

    slots = [
        StorageViewRuntimeSlot(
            slot_index=index,
            slot_label=f"Slot {index}",
            occupied=True,
            state="healthy",
            source="snapshot_slot" if index % live_every == 0 else "inventory_candidate",
            snapshot_slot=100 + index if index % live_every == 0 else None,
            device_name=f"da{index}",
            serial=f"SERIAL-{index:03d}",
        )
        for index in range(slot_count)
    ]
    view = StorageViewRuntimeView(
        id=VIEW_ID,
        label="Boot DOMs",
        kind="boot_devices",
        template_id="generic",
        backing_enclosure_id="enc-a",
        slots=slots,
    )
    return StorageViewRuntimePayload(system_id="system-a", views=[view])


def _inventory_service(runtime: StorageViewRuntimePayload) -> InventoryService:
    service = object.__new__(InventoryService)
    service.settings = Settings()
    service.system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="scale"))
    service._smart_operation_limit = 4
    service._smart_negative_cache = OrderedDict()
    service.get_storage_view_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
    service.get_slot_smart_summaries = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda slots, **_: [
            SmartBatchItem(slot=slot, summary=SmartSummaryView(available=True, temperature_c=slot))
            for slot in slots
        ]
    )
    service._get_slot_smart_summary_for_slot_view = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda slot_view, **_: SmartSummaryView(
            available=True,
            temperature_c=slot_view.slot,
        )
    )
    return service


class InventoryStorageViewBatchTests(unittest.TestCase):
    def test_live_slots_go_through_one_enclosure_batch_and_the_rest_load_directly(self) -> None:
        service = _inventory_service(_runtime(6))

        items = asyncio.run(
            service.get_storage_view_slot_smart_summaries(
                VIEW_ID,
                [0, 1, 2, 3, 4, 5],
                max_concurrency=3,
                allow_stale_cache=True,
            )
        )

        self.assertEqual([item.slot for item in items], [0, 1, 2, 3, 4, 5])
        service.get_storage_view_runtime.assert_awaited_once()
        service.get_slot_smart_summaries.assert_awaited_once()
        batch_call = service.get_slot_smart_summaries.await_args
        self.assertEqual(batch_call.args[0], [100, 102, 104])
        self.assertEqual(batch_call.kwargs["selected_enclosure_id"], "enc-a")
        self.assertEqual(batch_call.kwargs["max_concurrency"], 3)
        self.assertTrue(batch_call.kwargs["allow_stale_cache"])
        # Live slots carry the enclosure bay's summary, the others the direct load.
        by_slot = {item.slot: item.summary.temperature_c for item in items}
        self.assertEqual(by_slot[0], 100)
        self.assertEqual(by_slot[4], 104)
        self.assertEqual(service._get_slot_smart_summary_for_slot_view.await_count, 3)
        self.assertEqual(by_slot[1], 10_001)

    def test_unknown_and_duplicate_slot_indexes_are_skipped(self) -> None:
        service = _inventory_service(_runtime(4))

        items = asyncio.run(service.get_storage_view_slot_smart_summaries(VIEW_ID, [2, 2, 99, 0]))

        self.assertEqual([item.slot for item in items], [2, 0])

    def test_an_unknown_view_is_an_api_error(self) -> None:
        service = _inventory_service(_runtime(2))

        with self.assertRaises(TrueNASAPIError):
            asyncio.run(service.get_storage_view_slot_smart_summaries("missing", [0]))

    def test_a_failing_direct_slot_gets_a_fallback_summary_not_a_failed_batch(self) -> None:
        service = _inventory_service(_runtime(2))
        service._get_slot_smart_summary_for_slot_view = AsyncMock(  # type: ignore[method-assign]
            side_effect=TrueNASAPIError("no SSH")
        )

        items = asyncio.run(service.get_storage_view_slot_smart_summaries(VIEW_ID, [0, 1]))

        self.assertEqual([item.slot for item in items], [0, 1])
        self.assertIn("no SSH", items[1].summary.message or "")


class StorageViewBatchRouteTests(unittest.TestCase):
    def _call(self, service: Mock, slots: list[int], **query: object):
        route = _route("/api/storage-views/{view_id}/slots/smart-batch", "POST")
        registry = Mock()
        registry.get_service.return_value = service
        payload = Mock()
        payload.slots = slots
        payload.max_concurrency = None
        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            return asyncio.run(
                route.endpoint(view_id=VIEW_ID, payload=payload, system_id="system-a", **query)
            )

    def _service(self, **kwargs: object) -> Mock:
        service = Mock()
        service.system.id = "system-a"
        service.system.truenas.platform = "scale"
        service.get_storage_view_slot_smart_summaries = AsyncMock(**kwargs)
        return service

    def test_the_route_returns_the_batch_and_passes_the_fresh_flag(self) -> None:
        item = SmartBatchItem(slot=3, summary=SmartSummaryView(available=True))
        service = self._service(return_value=[item])

        response = self._call(service, [3], enclosure_id="enc-a", fresh=True)

        self.assertEqual(response.summaries, [item])
        call = service.get_storage_view_slot_smart_summaries.await_args
        self.assertEqual(call.args, (VIEW_ID, [3]))
        self.assertEqual(call.kwargs["selected_enclosure_id"], "enc-a")
        self.assertFalse(call.kwargs["allow_stale_cache"])
        self.assertTrue(call.kwargs["bypass_negative_cache"])

    def test_failures_map_to_the_same_statuses_as_the_enclosure_batch(self) -> None:
        cases = [
            (TrueNASAPIError('The saved view "x" does not exist on this system.'), 400),
            (OSError(errno.ECONNRESET, "reset"), 503),
            (OSError(errno.EACCES, "permission denied"), 500),
        ]
        for error, status in cases:
            with self.subTest(error=error):
                service = self._service(side_effect=error)
                with self.assertLogs("app.main", level="ERROR") if status == 500 else _no_logs():
                    with self.assertRaises(HTTPException) as raised:
                        self._call(service, [0])
                self.assertEqual(raised.exception.status_code, status)


class _no_logs:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


class CollectorStorageViewBatchTests(unittest.TestCase):
    def _collector(self, **settings: object) -> HistoryCollector:
        return HistoryCollector(
            HistorySettings(source_base_url="http://enclosure-ui:8000", request_timeout_seconds=45, **settings),
            Mock(),
        )

    @staticmethod
    def _scope() -> ScopeSnapshot:
        return ScopeSnapshot(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_id=f"storage-view:{VIEW_ID}",
            enclosure_label="Boot DOMs",
            snapshot={
                "storage_view_id": VIEW_ID,
                "storage_view_backing_enclosure_id": "enc-a",
                "slots": [],
            },
        )

    def test_a_sixty_slot_view_is_collected_in_three_requests(self) -> None:
        collector = self._collector()

        async def fake_fetch(path: str, **kwargs: object) -> dict[str, object]:
            slots = json.loads(kwargs["body"])["slots"]  # type: ignore[arg-type]
            return {"summaries": [{"slot": slot, "summary": {"available": True}} for slot in slots]}

        collector._fetch_json = AsyncMock(side_effect=fake_fetch)  # type: ignore[method-assign]

        summaries = asyncio.run(collector._fetch_smart_summaries(self._scope(), list(range(60))))

        self.assertEqual(sorted(summaries), list(range(60)))
        calls = collector._fetch_json.await_args_list  # type: ignore[attr-defined]
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertEqual(call.args[0], BATCH_PATH)
            self.assertEqual(call.kwargs["method"], "POST")
            self.assertEqual(call.kwargs["params"], {"system_id": "archive-core", "enclosure_id": "enc-a"})
        self.assertEqual([len(json.loads(call.kwargs["body"])["slots"]) for call in calls], [24, 24, 12])

    def test_fresh_collection_passes_fresh_and_no_short_timeout(self) -> None:
        collector = self._collector()
        collector._fetch_json = AsyncMock(return_value={"summaries": []})  # type: ignore[method-assign]

        asyncio.run(collector._fetch_smart_summaries(self._scope(), [0, 1], force_fresh=True))

        call = collector._fetch_json.await_args  # type: ignore[attr-defined]
        self.assertEqual(call.kwargs["params"]["fresh"], "true")
        self.assertIsNone(call.kwargs["timeout_seconds"])

    def test_an_older_main_ui_without_the_batch_route_falls_back_to_per_slot_requests(self) -> None:
        collector = self._collector()
        responses: list[object] = [
            HistorySourceError.rejected("POST batch failed with HTTP 405", status_code=405),
            {"available": True, "temperature_c": 30},
            {"available": True, "temperature_c": 31},
        ]

        async def fake_fetch(path: str, **kwargs: object) -> object:
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        collector._fetch_json = AsyncMock(side_effect=fake_fetch)  # type: ignore[method-assign]

        summaries = asyncio.run(collector._fetch_smart_summaries(self._scope(), [0, 1]))

        self.assertEqual(summaries[1]["temperature_c"], 31)
        paths = [call.args[0] for call in collector._fetch_json.await_args_list]  # type: ignore[attr-defined]
        self.assertEqual(
            paths,
            [
                BATCH_PATH,
                f"/api/storage-views/{VIEW_ID}/slots/0/smart",
                f"/api/storage-views/{VIEW_ID}/slots/1/smart",
            ],
        )

    def test_a_real_batch_failure_is_not_hidden_by_the_fallback(self) -> None:
        collector = self._collector()
        collector._fetch_json = AsyncMock(  # type: ignore[method-assign]
            side_effect=HistorySourceError.rejected("POST batch failed with HTTP 503", status_code=503)
        )

        with self.assertRaises(HistorySourceError):
            asyncio.run(collector._fetch_smart_summaries(self._scope(), [0, 1]))
        self.assertEqual(collector._fetch_json.await_count, 1)  # type: ignore[attr-defined]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
