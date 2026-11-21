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
from unittest.mock import AsyncMock, patch

from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView, SmartSummaryView
from app.services.inventory import (
    STABLE_SMART_DETAIL_FIELDS,
    InventoryService,
    SmartDetailBatch,
    slot_detail_collection_binding,
)
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore
from app.services.truenas_ws import TrueNASAPIError
from tests.inventory_fixtures import build_inventory_service

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


class LoaderPersistedUnreadMarkerTests(unittest.TestCase):
    """The same contract through the real SMART loader, CORE middleware path.

    A reply whose only stable field could not be read has nothing readable, so
    it is not a successful reading and never enters the reading cache. It is
    still an observation: the saved row must name the field as not read, or a
    restart serves the older measurement as if nothing had happened.
    """

    PLATFORM = "core"

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.directory = temp_dir.name

    def _service(self) -> InventoryService:
        # A new service and store on the same files: a restarted process.
        return build_inventory_service(
            Settings(), SystemConfig(id="synthetic", truenas=TrueNASConfig(platform=self.PLATFORM)),
            AsyncMock(), AsyncMock(), self.directory,
        )

    def _payload(self, rotation: object, temperature: int | None = None) -> dict:
        payload: dict[str, object] = {"rotation_rate": rotation}
        if temperature is not None:
            payload["temperature"] = {"current": temperature}
        return payload

    def _read(
        self, payload: dict, *, failure: Exception | None = None, allow_stale_cache: bool = False,
    ) -> tuple[InventoryService, SmartSummaryView]:
        service = self._service()
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, present=True,
                        serial=SERIAL, enclosure_id="enc-1", device_name="da0", smart_device_names=["da0"],
                        raw_status={"disk_raw": payload} if self.PLATFORM == "quantastor" else {})
        if failure is not None:
            service.truenas_client.fetch_disk_smartctl.side_effect = failure
        else:
            service.truenas_client.fetch_disk_smartctl.return_value = json.dumps(payload)
        service.get_snapshot = AsyncMock(return_value=InventorySnapshot(
            slots=[slot], refresh_interval_seconds=30, selected_enclosure_id="enc-1"))
        summary = asyncio.run(service.get_slot_smart_summary(0, "enc-1", allow_stale_cache=allow_stale_cache))
        return service, summary

    def _restarted(self) -> SmartSummaryView | None:
        return self._service().get_cached_slot_smart_summary_without_layout(0, "enc-1")

    def _seed(self) -> None:
        _service, seed = self._read(self._payload(7200))
        self.assertEqual(seed.rotation_rate_rpm, 7200)
        restarted = self._restarted()
        self.assertIsNotNone(restarted)
        self.assertEqual(restarted.rotation_rate_rpm, 7200)

    def _unread(self, temperature: int | None = None) -> tuple[InventoryService, SmartSummaryView]:
        rotation = -1 if self.PLATFORM == "quantastor" else True
        service, live = self._read(self._payload(rotation, temperature))
        self.assertIsNone(live.rotation_rate_rpm)
        self.assertIn("rotation_rate_rpm", live.unread_fields)
        return service, live

    def _assert_restart_reads_unread(self) -> None:
        restarted = self._restarted()
        self.assertIsNotNone(restarted)
        self.assertIsNone(restarted.rotation_rate_rpm,
                          "an explicit not-read observation must supersede the older reading")
        self.assertIn("rotation_rate_rpm", restarted.unread_fields)

    def test_an_unread_only_reply_is_saved_and_survives_a_restart(self) -> None:
        self._seed()
        service, live = self._unread()
        self.assertIs(live.available, False)
        # Nothing readable: no successful-reading cache entry, the reply stays a negative one.
        self.assertEqual(service._smart_cache, {})
        self.assertEqual(len(service._smart_negative_cache), 1)
        self._assert_restart_reads_unread()

    def test_a_readable_sibling_control_survives_a_restart(self) -> None:
        self._seed()
        service, live = self._unread(temperature=30)
        self.assertIsNot(live.available, False)
        self.assertEqual(len(service._smart_cache), 1)
        self._assert_restart_reads_unread()

    def test_a_failed_read_with_nothing_unread_keeps_the_older_row(self) -> None:
        # Not an observation of the disk: the saved reading is not replaced.
        self._seed()
        _service, live = self._read({}, failure=TrueNASAPIError("synthetic failure"))
        self.assertEqual(live.unread_fields, [])
        restarted = self._restarted()
        self.assertIsNotNone(restarted)
        self.assertEqual(restarted.rotation_rate_rpm, 7200)

    def test_an_unread_only_reply_for_a_replaced_generation_is_not_saved(self) -> None:
        self._seed()
        original = InventoryService._smart_cache_generation_token
        calls = {"n": 0}

        def moving_generation(service: InventoryService, cache_key: Any) -> Any:
            # The first read of the token is the loader's own; every later one
            # sees a newer generation, as if the disk changed mid-read.
            calls["n"] += 1
            token = original(service, cache_key)
            return token if calls["n"] == 1 else (token, "newer")

        with patch.object(InventoryService, "_smart_cache_generation_token", moving_generation):
            self._read(self._payload(-1 if self.PLATFORM == "quantastor" else True))
        restarted = self._restarted()
        self.assertIsNotNone(restarted)
        self.assertEqual(restarted.rotation_rate_rpm, 7200)

    def test_a_later_readable_reply_replaces_the_saved_marker(self) -> None:
        self._seed()
        self._unread()
        _service, live = self._read(self._payload(5400))
        self.assertEqual(live.rotation_rate_rpm, 5400)
        restarted = self._restarted()
        self.assertEqual((restarted.rotation_rate_rpm, restarted.unread_fields), (5400, []))

    def test_saved_row_admission_of_an_unread_only_reply_keeps_its_checks(self) -> None:
        service = self._service()
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, present=True,
                        serial=SERIAL, enclosure_id="enc-1", device_name="da0", smart_device_names=["da0"])
        cache_key = service._smart_cache_key(slot)
        token = service._smart_cache_generation_token(cache_key)
        unread = SmartSummaryView(available=False, unread_fields=["rotation_rate_rpm"])

        def admit(summary: SmartSummaryView, generation: Any = token) -> bool:
            return service._admit_smart_summary_to_saved_row(
                slot, summary, expected_generation=generation, cache_key=cache_key,
            )

        self.assertTrue(admit(unread))
        self.assertEqual(service._smart_cache, {}, "an unread-only reply is not a reading")
        with self.subTest("a newer generation"):
            self.assertFalse(admit(unread, (token, "newer")))
        with self.subTest("nothing unread"):
            self.assertFalse(admit(SmartSummaryView(available=False)))
        with self.subTest("only a non-stable name unread"):
            self.assertFalse(admit(SmartSummaryView(available=False, unread_fields=["temperature_c"])))
        with self.subTest("a disk that is no longer current"), patch.object(
            InventoryService, "_smart_disk_is_current", return_value=False,
        ):
            self.assertFalse(admit(unread))

    def test_a_saved_unread_only_row_does_not_stop_a_stale_allowed_live_read(self) -> None:
        # The saved row is not a reading: it never enters the reading cache, so
        # answering with it would leave nothing to schedule a refresh. After a
        # restart a stale-allowed request reads live and saves the new reading.
        self._seed()
        self._unread()
        _service, live = self._read(self._payload(5400), allow_stale_cache=True)
        self.assertEqual((live.rotation_rate_rpm, live.unread_fields), (5400, []))
        restarted = self._restarted()
        self.assertEqual((restarted.rotation_rate_rpm, restarted.unread_fields), (5400, []))


class QuantaStorLoaderPersistedUnreadMarkerTests(LoaderPersistedUnreadMarkerTests):
    """The same through the QuantaStor loader; its saved-path rules are unchanged."""

    PLATFORM = "quantastor"

    def _payload(self, rotation: object, temperature: int | None = None) -> dict:
        payload: dict[str, object] = {"rotationRate": rotation}
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def test_a_failed_read_with_nothing_unread_keeps_the_older_row(self) -> None:
        # QuantaStor reads from the inventory row, not from a middleware call.
        self._seed()
        _service, live = self._read({})
        self.assertIs(live.available, False)
        self.assertEqual(live.unread_fields, [])
        restarted = self._restarted()
        self.assertIsNotNone(restarted)
        self.assertEqual(restarted.rotation_rate_rpm, 7200)


if __name__ == "__main__":
    unittest.main()
