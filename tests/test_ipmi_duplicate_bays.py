"""Duplicate BMC bay numbers must not choose a bay's disk or identify-light target.

Two RAID controllers, or two shelves behind one controller, can each report a
drive at the same bay number. A profile's bare ``bmc-slot:N`` hint cannot tell
those drives apart. The bay must then stay unknown, the ambiguity must be
reported, and no BMC controller or drive coordinates may be published for it,
whatever order the BMC listed the drives in. Synthetic records only.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.config import EnclosureProfileConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import ManualMapping, SlotState
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.supermicro_bmc import BMCDriveRecord, BMCInventory

PROFILE_ID = "synthetic-two-bay"
SHELF_A_DRIVE = BMCDriveRecord(controller_id=0, physical_index=0, slot_number=0, enclosure_id="shelf-a",
                               serial="SYNTHETIC-A", health="ONLINE")
SHELF_B_DRIVE = BMCDriveRecord(controller_id=1, physical_index=0, slot_number=0, enclosure_id="shelf-b",
                               serial="SYNTHETIC-B", health="ONLINE")
SECOND_BAY_DRIVE = BMCDriveRecord(controller_id=1, physical_index=1, slot_number=1, enclosure_id="shelf-b",
                                  serial="SYNTHETIC-C", health="ONLINE")
BMC_COORDINATE_KEYS = ("bmc_controller_id", "bmc_physical_index", "bmc_slot_number", "bmc_enclosure_id")


class DuplicateBmcBayTests(unittest.TestCase):
    def correlate(self, platform, drives, *, hints=None, mapped_serial=None):
        profile = EnclosureProfileConfig(id=PROFILE_ID, label="Synthetic two bays", rows=1, columns=2,
                                         slot_layout=[[0, 1]], slot_hints=hints or {})
        system = SystemConfig(id="synthetic-system", truenas=TrueNASConfig(platform=platform),
                              default_profile_id=PROFILE_ID)
        settings = Settings(systems=[system], profiles=[profile])
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MappingStore(str(Path(temp_dir) / "slot_mappings.json"))
            if mapped_serial is not None:
                store.save_mapping(ManualMapping(system_id=system.id, enclosure_id=PROFILE_ID, slot=0,
                                                 serial=mapped_serial))
            service = InventoryService(settings, system, MagicMock(), MagicMock(), MagicMock(), store,
                                       ProfileRegistry(settings),
                                       SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")))
            warnings: list[str] = []
            slots, *_ = service._correlate(raw_data=None, ssh_data=ParsedSSHData(), warnings=warnings,
                                           bmc_inventory=BMCInventory(drives=list(drives)))
        return {slot.slot: slot for slot in slots}, warnings

    def assert_ambiguous_bay(self, bay, warnings, *, bmc_slot=0):
        self.assertIsNone(bay.serial)
        self.assertEqual(bay.state, SlotState.unknown)
        self.assertFalse(bay.led_supported)
        self.assertIsNone(bay.led_backend)
        for key in BMC_COORDINATE_KEYS:
            self.assertIsNone(bay.raw_status.get(key), key)
        observations = bay.raw_status.get("bmc_observations")
        self.assertEqual(sorted(item["serial"] for item in observations), ["SYNTHETIC-A", "SYNTHETIC-B"])
        self.assertEqual({item["admission"] for item in observations}, {"ambiguous-bay"})
        self.assertEqual(sorted(item["controller_id"] for item in observations), [0, 1])
        self.assertTrue(any(f"2 drives at bay number {bmc_slot}" in warning and "ambiguous" in warning
                            for warning in warnings), warnings)

    def test_bmc_slot_hint_with_duplicate_bay_number_stays_unknown_in_both_orders(self):
        for order in ((SHELF_A_DRIVE, SHELF_B_DRIVE), (SHELF_B_DRIVE, SHELF_A_DRIVE)):
            with self.subTest(first=order[0].serial):
                bays, warnings = self.correlate("ipmi", order, hints={0: ["bmc-slot:0"]})
                self.assert_ambiguous_bay(bays[0], warnings)

    def test_unique_bmc_bay_is_still_admitted_with_its_identify_target(self):
        bays, warnings = self.correlate("ipmi", (SHELF_A_DRIVE, SECOND_BAY_DRIVE),
                                        hints={0: ["bmc-slot:0"], 1: ["bmc-slot:1"]})
        self.assertEqual(bays[0].serial, "SYNTHETIC-A")
        self.assertEqual(bays[0].mapping_source, "bmc-slot")
        self.assertTrue(bays[0].led_supported)
        self.assertEqual(bays[0].led_backend, "supermicro_bmc")
        self.assertEqual(bays[0].raw_status.get("bmc_controller_id"), 0)
        self.assertEqual(bays[1].serial, "SYNTHETIC-C")
        self.assertEqual(bays[1].raw_status.get("bmc_controller_id"), 1)
        self.assertFalse(any("bay number" in warning for warning in warnings), warnings)

    def test_reported_order_numbering_does_not_choose_between_duplicate_bay_numbers(self):
        for order in ((SHELF_A_DRIVE, SHELF_B_DRIVE, SECOND_BAY_DRIVE),
                      (SECOND_BAY_DRIVE, SHELF_B_DRIVE, SHELF_A_DRIVE)):
            with self.subTest(first=order[0].serial):
                bays, warnings = self.correlate("ipmi", order)
                self.assert_ambiguous_bay(bays[0], warnings)
                self.assertEqual(bays[1].serial, "SYNTHETIC-C")
                self.assertTrue(bays[1].led_supported)
                self.assertEqual(bays[1].raw_status.get("bmc_physical_index"), 1)

    def test_unmatched_saved_mapping_does_not_fall_back_to_the_shared_bay_hint(self):
        for order in ((SHELF_A_DRIVE, SHELF_B_DRIVE), (SHELF_B_DRIVE, SHELF_A_DRIVE)):
            with self.subTest(first=order[0].serial):
                bays, warnings = self.correlate("ipmi", order, hints={0: ["bmc-slot:0"]},
                                                mapped_serial="SYNTHETIC-MISSING")
                self.assertNotIn(bays[0].serial, {"SYNTHETIC-A", "SYNTHETIC-B"})
                self.assertFalse(bays[0].led_supported)
                for key in BMC_COORDINATE_KEYS:
                    self.assertIsNone(bays[0].raw_status.get(key), key)

    def test_saved_mapping_names_one_of_the_duplicates_regardless_of_order(self):
        for order in ((SHELF_A_DRIVE, SHELF_B_DRIVE), (SHELF_B_DRIVE, SHELF_A_DRIVE)):
            with self.subTest(first=order[0].serial):
                bays, _ = self.correlate("ipmi", order, hints={0: ["bmc-slot:0"]}, mapped_serial="SYNTHETIC-A")
                self.assertEqual(bays[0].serial, "SYNTHETIC-A")
                self.assertEqual(bays[0].mapping_source, "manual")
                self.assertFalse(bays[0].led_supported)
                self.assertIsNone(bays[0].raw_status.get("bmc_controller_id"))

    def test_esxi_reports_the_same_ambiguity_as_unknown_not_empty(self):
        for order in ((SHELF_A_DRIVE, SHELF_B_DRIVE), (SHELF_B_DRIVE, SHELF_A_DRIVE)):
            with self.subTest(first=order[0].serial):
                bays, warnings = self.correlate("esxi", order, hints={0: ["bmc-slot:0"]})
                self.assert_ambiguous_bay(bays[0], warnings)


if __name__ == "__main__":
    unittest.main()
