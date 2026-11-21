"""Saved slot details keep every stable SMART field's "not read" marker (#1294).

A SMART field the source reported in a shape that could not be read is "not
read": neither absent nor an older reading. The live summary says so in
``unread_fields``. The saved slot detail entry kept that marker only for
power-on time, so after a restart any other stable field (rotation rate,
firmware, block size, ...) came back absent, or as the previous disk read's
value. Every reader here starts from a fresh service and a fresh store object
on the same file, the way a restarted process does. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import SlotView, SmartSummaryView
from app.services.inventory import (
    STABLE_SMART_DETAIL_FIELDS,
    InventoryService,
    SmartDetailBatch,
    slot_detail_collection_binding,
)
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore

SERIAL = "SER-SYNTHETIC-1294"


class PersistedUnreadMarkerTests(unittest.TestCase):
    PLATFORM = "core"

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = Path(temp_dir.name) / "slot_detail_cache.json"

    def _system(self) -> SystemConfig:
        return SystemConfig(id="synthetic", truenas=TrueNASConfig(platform=self.PLATFORM))

    def _slot(self, **fields: Any) -> SlotView:
        return SlotView(slot=0, slot_label="00", row_index=0, column_index=0, enclosure_id="enc-1",
                        present=True, serial=SERIAL, **fields)

    def _service(self) -> InventoryService:
        # A fresh store object on the same file each time: nothing is shared in memory.
        service = object.__new__(InventoryService)
        service.system = self._system()
        service.settings = Settings()
        service.slot_detail_store = SlotDetailStore(str(self.path))
        service._smart_cache = {}
        service._smart_cache_until = {}
        service._smart_negative_cache = {}
        service._identity_unknown_slots = set()
        service._observe_smart_summary_request = lambda _label: None
        return service

    def _save(self, summary: SmartSummaryView, slot: SlotView | None = None) -> SlotDetailCacheEntry:
        service = self._service()
        entry = service._build_slot_detail_entry(slot or self._slot(), smart_summary=summary)
        self.assertIsNotNone(entry)

        # Save it the way a SMART read does: one request batch, one flush.
        async def flush() -> None:
            batch = SmartDetailBatch(service.slot_detail_store)
            batch.pending.append(entry)
            await batch.flush()

        asyncio.run(flush())
        return entry

    def _parse(self, payload: dict) -> SmartSummaryView:
        slot = self._slot()
        return InventoryService._merge_smart_summary(
            slot, InventoryService._parse_smart_payload_for_slot(slot, json.dumps(payload)),
        )

    def _store(self, smart_fields: dict) -> None:
        SlotDetailStore(str(self.path)).save_entries([SlotDetailCacheEntry(
            system_id="synthetic", enclosure_id="enc-1", slot=0, identifiers=[SERIAL],
            slot_fields={"serial": SERIAL}, smart_fields=smart_fields,
            collection_binding=slot_detail_collection_binding(self._system()),
        )])

    def _readers(self) -> dict[str, SmartSummaryView | None]:
        return {
            "rebuild": self._service()._build_persisted_smart_summary(self._slot()),
            "no-layout": self._service().get_cached_slot_smart_summary_without_layout(0, "enc-1"),
            "no-layout-batch": self._service().get_cached_slot_smart_summaries_without_layout(
                [0, 1], "enc-1",
            ).get(0),
        }

    def _assert_unread_after_restart(self, name: str) -> None:
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader, field=name):
                self.assertIsNotNone(summary)
                self.assertIsNone(getattr(summary, name))
                self.assertIn(name, summary.unread_fields)

    # -- the reproduction from the issue ---------------------------------

    def test_unread_rotation_rate_beside_readable_fields_survives_a_restart(self) -> None:
        self._save(self._parse({"serial_number": SERIAL, "rotation_rate": 7200, "firmware_version": "FW01"}))
        live = self._parse({
            "serial_number": SERIAL, "rotation_rate": True, "temperature": {"current": 30},
            "firmware_version": "FW01", "smart_status": {"passed": True},
        })
        self.assertIsNone(live.rotation_rate_rpm)
        self.assertIn("rotation_rate_rpm", live.unread_fields)

        entry = self._save(live)
        self.assertEqual(entry.smart_fields.get("unread_fields"), ["rotation_rate_rpm"])
        self.assertNotIn("rotation_rate_rpm", entry.smart_fields)
        self._assert_unread_after_restart("rotation_rate_rpm")
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual(summary.firmware_version, "FW01")

    def test_unread_rotation_rate_alone_replaces_the_older_reading(self) -> None:
        # No readable stable sibling: the entry writer used to find nothing to
        # save, so it carried the previous read's 7200 forward as stale.
        self._save(self._parse({"serial_number": SERIAL, "rotation_rate": 7200, "firmware_version": "FW01"}))
        live = self._parse({"serial_number": SERIAL, "rotation_rate": True, "temperature": {"current": 30}})
        self.assertEqual(live.unread_fields, ["rotation_rate_rpm"])

        entry = self._save(live)
        self.assertEqual(entry.smart_fields.get("unread_fields"), ["rotation_rate_rpm"])
        self.assertNotIn("rotation_rate_rpm", entry.smart_fields)
        self.assertFalse(entry.smart_stale)
        self._assert_unread_after_restart("rotation_rate_rpm")

    # -- every stable field ----------------------------------------------

    def test_every_stable_field_marker_is_written_and_read_back(self) -> None:
        for name in STABLE_SMART_DETAIL_FIELDS:
            readable = "form_factor" if name == "firmware_version" else "firmware_version"
            summary = SmartSummaryView(available=True, unread_fields=[name], **{readable: "SYN"})
            entry = self._save(summary)
            with self.subTest(field=name, step="write"):
                self.assertEqual(entry.smart_fields.get("unread_fields"), [name])
            self._assert_unread_after_restart(name)

    def test_several_markers_are_written_sorted(self) -> None:
        summary = SmartSummaryView(
            available=True, firmware_version="FW01",
            unread_fields=["rotation_rate_rpm", "logical_block_size", "power_on_hours", "power_on_days"],
        )
        entry = self._save(summary)
        self.assertEqual(entry.smart_fields.get("unread_fields"),
                         ["logical_block_size", "power_on_days", "power_on_hours", "rotation_rate_rpm"])
        for name in ("logical_block_size", "power_on_days", "power_on_hours", "rotation_rate_rpm"):
            self._assert_unread_after_restart(name)

    def test_a_non_stable_marker_is_still_not_written(self) -> None:
        # The saved reader shows stable fields only; a live-only field's
        # marker has nothing to describe after a restart.
        summary = SmartSummaryView(available=True, firmware_version="FW01",
                                   unread_fields=["temperature_c", "bytes_read"])
        entry = self._save(summary)
        self.assertNotIn("unread_fields", entry.smart_fields)

    def test_a_marker_beside_a_real_value_is_not_written(self) -> None:
        summary = SmartSummaryView(available=True, firmware_version="FW01", rotation_rate_rpm=7200,
                                   unread_fields=["rotation_rate_rpm"])
        entry = self._save(summary)
        self.assertNotIn("unread_fields", entry.smart_fields)
        self.assertEqual(entry.smart_fields.get("rotation_rate_rpm"), 7200)

    def test_a_real_reading_after_a_refusal_clears_the_stored_marker(self) -> None:
        self._save(self._parse({"serial_number": SERIAL, "rotation_rate": True, "firmware_version": "FW01"}))
        self._assert_unread_after_restart("rotation_rate_rpm")
        entry = self._save(self._parse({"serial_number": SERIAL, "rotation_rate": 7200, "firmware_version": "FW01"}))
        self.assertNotIn("unread_fields", entry.smart_fields)
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual((summary.rotation_rate_rpm, summary.unread_fields), (7200, []))

    # -- files written before this change ---------------------------------

    def test_an_old_file_without_markers_still_loads(self) -> None:
        self._store({"available": True, "rotation_rate_rpm": 7200, "firmware_version": "FW01"})
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertIsNotNone(summary)
                self.assertEqual((summary.rotation_rate_rpm, summary.firmware_version, summary.unread_fields),
                                 (7200, "FW01", []))

    def test_a_stored_non_stable_or_unknown_name_is_dropped(self) -> None:
        self._store({"available": True, "firmware_version": "FW01",
                     "unread_fields": ["temperature_c", "not_a_field", "rotation_rate_rpm"]})
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual(summary.unread_fields, ["rotation_rate_rpm"])

    def test_a_stored_marker_beside_a_stored_value_is_dropped(self) -> None:
        self._store({"available": True, "rotation_rate_rpm": 7200, "unread_fields": ["rotation_rate_rpm"]})
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual((summary.rotation_rate_rpm, summary.unread_fields), (7200, []))


class QuantaStorPersistedUnreadMarkerTests(PersistedUnreadMarkerTests):
    """The same contract through the QuantaStor summary builder.

    Saved SMART paths and the saved device name stay unrestored there; this
    only checks that a refused rotation rate keeps its marker across a restart.
    """

    PLATFORM = "quantastor"

    def test_a_refused_quantastor_rotation_rate_stays_unread_after_a_restart(self) -> None:
        def summary(**disk: object) -> SmartSummaryView:
            slot = self._slot(raw_status={"disk_raw": disk})
            service = self._service()
            return service._merge_smart_summary(slot, service._build_quantastor_smart_summary(slot))

        self._save(summary(smartHealthTest="PASSED", rotationRate=7200, firmwareVersion="FW01"))
        live = summary(smartHealthTest="PASSED", rotationRate=-1, firmwareVersion="FW01")
        self.assertIsNone(live.rotation_rate_rpm)
        self.assertIn("rotation_rate_rpm", live.unread_fields)
        entry = self._save(live)
        self.assertEqual(entry.smart_fields.get("unread_fields"), ["rotation_rate_rpm"])
        self._assert_unread_after_restart("rotation_rate_rpm")


if __name__ == "__main__":
    unittest.main()
