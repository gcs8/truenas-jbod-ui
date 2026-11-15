"""QuantaStor keeps the strength of the SES occupancy evidence it projects (#1214).

QuantaStor correlation copies the selected shelf's SES candidate into its own
slot status. The copy used to keep ``present`` but drop ``presence_source``
and ``presence_conflict``, so a weaker zero SAS hint was re-read as a live
empty report and hid a known disk behind ``mapping_source="ses-empty"``.
Synthetic inputs only; no appliance is contacted.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.config import Settings, SystemConfig, TrueNASConfig
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import (
    ParsedSSHData,
    SESMapEnclosure,
    SESMapSlot,
    build_slot_candidates_from_ses_enclosures,
)
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import TrueNASRawData

SHELF_ID = "5eeeee10"


def _raw() -> TrueNASRawData:
    return TrueNASRawData(
        enclosures=[],
        systems=[{"id": "node-a"}],
        hw_enclosures=[{"id": SHELF_ID, "storageSystemId": "node-a"}],
        disks=[{"id": "disk-a", "storageSystemId": "node-a", "devicePath": "/dev/sda",
                "serialNumber": "SANITIZED-A", "slot": 0, "healthStatus": "ONLINE"}],
        pools=[],
        disk_temperatures={},
        smart_test_results=[],
    )


def _bay(mode: str) -> SESMapSlot:
    bay = SESMapSlot(slot_number=0, element_id=0, ses_device="/dev/sg2",
                     slot_number_source="ses_device_slot_number",
                     sas_address="0", sas_address_source="sg_ses_aes",
                     sas_device_type="no SAS device attached")
    if mode == "sysfs-present-over-zero-sas":
        bay.present, bay.presence_source = True, "enclosure_sysfs"
        bay.device_names, bay.device_names_source = ["sda"], "enclosure_sysfs"
    elif mode == "equal-strength-presence-conflict":
        bay.present, bay.presence_source, bay.presence_conflict = True, "sg_ses_aes", True
        bay.device_names, bay.device_names_source = ["sda"], "enclosure_sysfs"
    elif mode == "explicit-empty":
        bay.present, bay.presence_source = False, "sg_ses_aes"
    elif mode == "unknown-presence":
        bay.sas_address = bay.sas_address_source = bay.sas_device_type = None
    else:
        raise AssertionError(mode)
    return bay


def _overlay(mode: str) -> tuple[ParsedSSHData, dict]:
    enclosure = SESMapEnclosure(enclosure_id=SHELF_ID, ses_device="/dev/sg2", slots={0: _bay(mode)})
    candidates, _meta = build_slot_candidates_from_ses_enclosures([enclosure], 2, None)
    return ParsedSSHData(ses_enclosures=[enclosure], ses_slot_candidates=candidates), candidates[0]


class QuantastorSesProjectionTests(unittest.TestCase):
    def service(self) -> InventoryService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        system = SystemConfig(id="quantastor-ses-projection",
                              default_profile_id="supermicro-ssg-2028r-shared-front-24",
                              truenas=TrueNASConfig(platform="quantastor"))
        service = InventoryService(
            settings, system, MagicMock(), MagicMock(), None,
            MappingStore(str(Path(directory.name) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(directory.name) / "slot_detail_cache.json")),
        )
        service.mapping_store.get_mapping = MagicMock(return_value=None)  # type: ignore[method-assign]
        return service

    def bay_zero(self, service: InventoryService, overlay: ParsedSSHData):
        slots, *_rest = service._correlate_quantastor(_raw(), [], "node-a", overlay)
        return slots[0]

    def test_baseline_without_ses_identifies_the_disk(self) -> None:
        slot = self.bay_zero(self.service(), ParsedSSHData())
        self.assertEqual(slot.serial, "SANITIZED-A")
        self.assertEqual(slot.mapping_source, "api-slot")
        self.assertEqual(slot.state.value, "healthy")

    def test_projection_keeps_presence_strength_and_conflict(self) -> None:
        for mode in ("sysfs-present-over-zero-sas", "equal-strength-presence-conflict"):
            with self.subTest(mode):
                _overlay_data, candidate = _overlay(mode)
                self.assertFalse(InventoryService._slot_has_live_empty_evidence(candidate))
                projected: dict = {}
                InventoryService._merge_quantastor_ses_candidate(projected, candidate)
                self.assertIs(projected["present"], True)
                self.assertEqual(projected.get("presence_source"), candidate["presence_source"])
                self.assertEqual(projected.get("presence_conflict"), candidate["presence_conflict"])
                self.assertEqual(projected.get("sas_address_source"), "sg_ses_aes")
                # One occupancy decision: the projection reaches the same answer.
                self.assertFalse(InventoryService._slot_has_live_empty_evidence(projected))

    def test_stronger_presence_keeps_the_disk_in_its_bay(self) -> None:
        for mode in ("sysfs-present-over-zero-sas", "equal-strength-presence-conflict"):
            with self.subTest(mode):
                overlay, candidate = _overlay(mode)
                slot = self.bay_zero(self.service(), overlay)
                self.assertNotEqual(slot.mapping_source, "ses-empty")
                self.assertEqual(slot.mapping_source, "api-slot")
                self.assertEqual(slot.serial, "SANITIZED-A")
                self.assertEqual(slot.device_name, "sda")
                self.assertTrue(slot.present)
                self.assertEqual(slot.state.value, "healthy")
                self.assertEqual(slot.raw_status.get("presence_source"), candidate["presence_source"])
                self.assertEqual(slot.raw_status.get("presence_conflict"), candidate["presence_conflict"])

    def test_explicit_empty_still_wins(self) -> None:
        # Control: sourced AES absence over a zero SAS hint is a live empty report.
        overlay, candidate = _overlay("explicit-empty")
        projected: dict = {}
        InventoryService._merge_quantastor_ses_candidate(projected, candidate)
        self.assertTrue(InventoryService._slot_has_live_empty_evidence(candidate))
        self.assertTrue(InventoryService._slot_has_live_empty_evidence(projected))
        slot = self.bay_zero(self.service(), overlay)
        self.assertEqual(slot.mapping_source, "ses-empty")
        self.assertIsNone(slot.serial)
        self.assertFalse(slot.present)

    def test_unknown_ses_presence_is_not_empty_evidence(self) -> None:
        # SES reached the bay but decided nothing about occupancy.
        overlay, candidate = _overlay("unknown-presence")
        self.assertIsNone(candidate.get("present"))
        slot = self.bay_zero(self.service(), overlay)
        self.assertNotEqual(slot.mapping_source, "ses-empty")
        self.assertEqual(slot.serial, "SANITIZED-A")
        self.assertEqual(slot.state.value, "healthy")


if __name__ == "__main__":
    unittest.main()
