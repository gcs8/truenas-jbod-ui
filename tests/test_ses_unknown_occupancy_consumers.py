"""A bay whose occupancy was not read is unknown in every consumer (#1218).

The slot view keeps the legacy ``present`` boolean (False for an unread bay)
and says separately whether occupancy was read: ``presence_known``. Consumers
read that fact instead of treating the legacy False as an empty bay:

- a status keyword such as ``OK`` does not turn an unread SES bay occupied;
- the SES storage-view runtime does not mark an unread bay occupied;
- history keeps the last recorded occupancy and disk for an unread bay, writes
  no present-to-absent event for it, and does not call a SMART pass complete
  while the disk last recorded there was not read; earlier alert evidence is
  kept until a real read replaces it.

Real parsers, resolvers, slot-view builders, the history collector and a
disposable SQLite history store; synthetic inputs only.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import StorageViewConfig
from app.models.domain import InventorySnapshot, ManualMapping, SlotView
from history_service.collector import HistoryCollector, ScopeSnapshot
from history_service.config import HistorySettings
from history_service.domain import SlotStateRecord
from history_service.store import HistoryStore
# Module imports, not class imports: a TestCase class bound in this module
# would be collected and run a second time here.
from tests import test_quantastor_ses_projection as quantastor_cases
from tests import test_ses_unknown_presence as presence_cases

MAPPING = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
FAILED_HOT = {0: {"available": True, "smart_health_status": "FAILED", "temperature_c": 61}}
PASSED_COOL = {0: {"available": True, "smart_health_status": "PASSED", "temperature_c": 35}}


def _core_view(row: str, mapping: ManualMapping | None = None, disk: Any = None) -> SlotView:
    case = presence_cases.CoreSesutilShowPresenceTests()
    try:
        _resolution, view = case.resolve(case.service(), presence_cases._show_candidate(row), mapping, disk)
    finally:
        case.doCleanups()
    return view


def _quantastor_unknown_view(mapped: bool) -> SlotView:
    case = quantastor_cases.QuantastorSesProjectionTests()
    try:
        service = case.service()
        if mapped:
            service.mapping_store.get_mapping.return_value = MAPPING  # type: ignore[attr-defined]
        overlay, _candidate = quantastor_cases._overlay("unknown-presence")
        return case.bay_zero_without_api_disk(service, overlay)
    finally:
        case.doCleanups()


def _payload(view: SlotView) -> dict[str, Any]:
    # The collector reads the inventory API's JSON, not the model object.
    payload = json.loads(view.model_dump_json())
    payload["enclosure_id"] = "enc-a"
    return payload


KNOWN_PRESENT = lambda: _core_view("Slot 00  da0  SANITIZED-MODEL  SANITIZED-MAPPED  OK", MAPPING, presence_cases._disk())  # noqa: E731
KNOWN_EMPTY = lambda: _core_view("Slot 00  -  -  -  Not installed")  # noqa: E731
UNREAD = {
    "core-show-unknown": lambda: _core_view("Slot 00  -  -  -  Unknown"),
    "core-show-ok": lambda: _core_view("Slot 00  -  -  -  OK"),
    "quantastor-unknown": lambda: _quantastor_unknown_view(mapped=False),
    "quantastor-unresolved-mapping": lambda: _quantastor_unknown_view(mapped=True),
}


class SlotViewOccupancyTests(unittest.TestCase):
    def test_unread_bays_are_neither_occupied_nor_empty(self) -> None:
        for name, build in UNREAD.items():
            with self.subTest(name):
                view = build()
                self.assertIs(view.presence_known, False)
                self.assertIs(view.present, False)
                self.assertNotIn(view.state.value, {"empty", "healthy"})
                self.assertIs(_payload(view)["presence_known"], False)
                record = SlotStateRecord.from_snapshot_slot({"selected_system_id": "s"}, _payload(view))
                self.assertIs(record.presence_known, False)

    def test_status_ok_does_not_make_an_unread_ses_bay_occupied(self) -> None:
        # `Slot 00 - - - OK`: no device name, no disk. The parser left
        # occupancy unknown; the OK keyword must not decide it afterwards.
        view = _core_view("Slot 00  -  -  -  OK")
        self.assertIs(view.present, False)
        self.assertIs(view.presence_known, False)
        self.assertEqual(view.state.value, "unknown")

    def test_decided_bays_keep_known_occupancy(self) -> None:
        present = KNOWN_PRESENT()
        self.assertEqual((present.present, present.presence_known, present.state.value), (True, True, "healthy"))
        empty = KNOWN_EMPTY()
        self.assertEqual((empty.present, empty.presence_known, empty.state.value), (False, True, "empty"))
        # Known occupancy is the default, so it is left out of the JSON and
        # saved snapshots without the field read as before.
        self.assertNotIn("presence_known", _payload(present))
        self.assertNotIn("presence_known", _payload(empty))
        self.assertIs(SlotView(slot=0, slot_label="00", row_index=0, column_index=0).presence_known, True)

    def test_status_ok_without_ses_evidence_still_reads_occupied(self) -> None:
        # Control: an appliance status with no SES record behind it keeps its
        # meaning, so the guard is limited to undecided SES occupancy.
        case = presence_cases.CoreSesutilShowPresenceTests()
        try:
            service = case.service()
            from app.services.parsers import parse_ssh_outputs
            view = service._build_slot_view(
                0, 0, 0, {}, {"status": "OK"}, None, None, parse_ssh_outputs({}, 1, None), {}, set(),
            )
        finally:
            case.doCleanups()
        self.assertIs(view.present, True)
        self.assertIs(view.presence_known, True)

    def test_ses_storage_view_runtime_does_not_mark_an_unread_bay_occupied(self) -> None:
        case = presence_cases.CoreSesutilShowPresenceTests()
        try:
            service = case.service()
            view = StorageViewConfig(id="front", label="Front", kind="ses_enclosure", template_id="ses-auto")
            for name, build in {**UNREAD, "known-present": KNOWN_PRESENT, "known-empty": KNOWN_EMPTY}.items():
                with self.subTest(name):
                    slot = build()
                    snapshot = InventorySnapshot(slots=[slot], refresh_interval_seconds=60)
                    runtime = service._build_ses_storage_view_runtime(view, snapshot)
                    runtime_slot = next(item for item in runtime.slots if item.slot_index == 0)
                    self.assertIs(runtime_slot.occupied, name == "known-present")
                    self.assertEqual(runtime_slot.state, slot.state.value)
                    # The page draws a runtime tile from this flag when the
                    # view is backed by another enclosure than the snapshot.
                    unread = name in UNREAD
                    self.assertIs(runtime_slot.presence_known, not unread)
                    self.assertIs("presence_known" in json.loads(runtime_slot.model_dump_json()), unread)
        finally:
            case.doCleanups()


class UnreadBayHistoryTests(unittest.TestCase):
    def collector(self) -> tuple[HistoryCollector, HistoryStore]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = HistoryStore(str(root / "history.db"))
        store.create_backup = MagicMock(return_value=None)  # type: ignore[method-assign]
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(root / "history.db"),
                backup_dir=str(root / "backups"),
                poll_interval_seconds=300,
                failure_backoff_max_seconds=900,
                startup_grace_seconds=0,
            ),
            store,
        )
        return collector, store

    def run_pass(self, collector: HistoryCollector, view: SlotView, summaries: dict, at: datetime) -> tuple[dict, int]:
        collector._enumerate_scopes = AsyncMock(return_value=[ScopeSnapshot(  # type: ignore[method-assign]
            system_id="s", system_label="S", enclosure_id="enc-a", enclosure_label="Shelf",
            snapshot={"selected_system_id": "s", "selected_enclosure_id": "enc-a", "slots": [_payload(view)]},
        )])
        fetch = AsyncMock(return_value=summaries)
        collector._fetch_smart_summaries = fetch  # type: ignore[method-assign]
        with patch("history_service.collector.utcnow", return_value=at):
            asyncio.run(collector.run_once(force_fast=True, include_due_intervals=False))
        return collector.status(), fetch.await_count

    @staticmethod
    def presence_changes(store: HistoryStore) -> list[tuple[Any, Any]]:
        changes = []
        for event in reversed(store.list_slot_events("s", "enc-a", 0)):
            details = json.loads(event.get("details_json") or "{}")
            if "present" in details:
                changes.append((details["present"]["previous"], details["present"]["current"]))
        return changes

    def test_unread_bay_keeps_its_recorded_disk_and_alert_evidence(self) -> None:
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        for name, build in UNREAD.items():
            with self.subTest(name):
                collector, store = self.collector()
                first, _calls = self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
                self.assertEqual(first["last_smart_failure_evidence_disks"], 1)
                unread, calls = self.run_pass(collector, build(), {}, start + timedelta(minutes=5))
                self.assertEqual(calls, 0)
                # The failed disk was not read again: its evidence stands.
                self.assertEqual(unread["last_smart_failure_evidence_disks"], 1)
                self.assertEqual(unread["last_max_temperature_celsius"], 61)
                self.assertEqual(unread["last_smart_failure_evidence_at"], first["last_smart_failure_evidence_at"])
                self.assertEqual(unread["last_temperature_evidence_at"], first["last_temperature_evidence_at"])
                row = store.get_slot_states("s", "enc-a")[0]
                self.assertIs(row.present, True)
                self.assertEqual(row.serial, "SANITIZED-MAPPED")
                self.assertEqual(self.presence_changes(store), [])

    def test_unread_then_known_empty_records_one_removal(self) -> None:
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        collector, store = self.collector()
        self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
        self.run_pass(collector, UNREAD["core-show-unknown"](), {}, start + timedelta(minutes=5))
        empty, calls = self.run_pass(collector, KNOWN_EMPTY(), {}, start + timedelta(minutes=10))
        self.assertEqual(calls, 0)
        # A real read says the bay is empty: the removal and the cleared
        # evidence are both real now.
        self.assertIs(store.get_slot_states("s", "enc-a")[0].present, False)
        self.assertEqual(self.presence_changes(store), [(True, False)])
        self.assertEqual(empty["last_smart_failure_evidence_disks"], 0)
        self.assertIsNone(empty["last_max_temperature_celsius"])

    def test_unread_then_known_healthy_replaces_the_evidence(self) -> None:
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        collector, store = self.collector()
        self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
        self.run_pass(collector, UNREAD["quantastor-unknown"](), {}, start + timedelta(minutes=5))
        healthy, calls = self.run_pass(collector, KNOWN_PRESENT(), PASSED_COOL, start + timedelta(minutes=10))
        self.assertEqual(calls, 1)
        self.assertEqual(healthy["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(healthy["last_max_temperature_celsius"], 35)
        self.assertIs(store.get_slot_states("s", "enc-a")[0].present, True)
        self.assertEqual(self.presence_changes(store), [])

    def test_unread_bay_without_a_recorded_disk_stores_nothing(self) -> None:
        collector, store = self.collector()
        self.run_pass(collector, UNREAD["core-show-unknown"](), {}, datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(store.get_slot_states("s", "enc-a"), {})

    def test_known_empty_control_clears_without_an_unread_bay(self) -> None:
        # Control: a real empty read clears the evidence in the same pass shape.
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        collector, store = self.collector()
        self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
        empty, _calls = self.run_pass(collector, KNOWN_EMPTY(), {}, start + timedelta(minutes=5))
        self.assertEqual(empty["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(self.presence_changes(store), [(True, False)])


if __name__ == "__main__":
    unittest.main()
