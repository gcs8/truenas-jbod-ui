"""Unknown SES occupancy stays unknown until evidence decides it (#1218).

A truncated AES descriptor, or an Enclosure Status record that carries only a
condition code, says nothing about whether a bay holds a disk. The parsed bay
keeps ``present=None``; the inventory candidate must keep it too, so the
resolver does not read it as a live empty report and drop a validly mapped
disk. Explicit absence (``Not installed``, ``no SAS device attached``) still
wins over a stored mapping. Synthetic inputs only.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import ManualMapping
from app.services.inventory import DiskRecord, InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import parse_sesutil_show_enclosures, parse_ssh_outputs
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore

AES_COMMAND = "sg_ses -p aes /dev/sg0"
EC_COMMAND = "sg_ses ec /dev/sg0"
AES_HEADER = "Element type: Array device slot\nElement index: 0\ndevice slot number: 0\n"
EC_HEADER = (
    "Enclosure status diagnostic page:\n"
    "  Element type: Array device slot, subenclosure id: 0 [ti=0]\n"
    "    Element 0 descriptor:\n"
)


def _ec(status: str) -> dict[str, str]:
    return {EC_COMMAND: EC_HEADER + f"      Predicted failure=0, Disabled=0, Swap=0, status: {status}\n"}


UNKNOWN_INPUTS = {
    "truncated-aes": {AES_COMMAND: AES_HEADER},
    "aes-blank-device-type": {AES_COMMAND: AES_HEADER + "SAS device type: \n"},
    "ec-critical": _ec("Critical"),
    "ec-noncritical": _ec("Noncritical"),
    "ec-unknown": _ec("Unknown"),
    "ec-unsupported": _ec("Unsupported"),
}
EMPTY_INPUTS = {
    "ec-not-installed": _ec("Not installed"),
    "aes-no-device": {AES_COMMAND: AES_HEADER + "SAS device type: no SAS device attached\nSAS address: 0x0\n"},
}


def _candidate(outputs: dict[str, str]) -> dict:
    parsed = parse_ssh_outputs(outputs, 1, None)
    return parsed.ses_slot_candidates[0]


def _disk() -> DiskRecord:
    values = {field.name: None for field in fields(DiskRecord)}
    values.update(raw={}, device_name="sda", serial="SANITIZED-MAPPED", smart_devices=[],
                  lookup_keys={"sanitized-mapped"})
    return DiskRecord(**values)


class UnknownSesPresenceTests(unittest.TestCase):
    def service(self) -> InventoryService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        system = SystemConfig(id="unknown-presence", truenas=TrueNASConfig(platform="scale"),
                              ssh=SSHConfig(enabled=False))
        return InventoryService(
            settings, system, AsyncMock(), AsyncMock(), None,
            MappingStore(str(Path(directory.name) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(directory.name) / "slot_detail_cache.json")),
        )

    def resolve(self, service, candidate, mapping, disk):
        disks = {"sanitized-mapped": disk} if disk else {}
        resolution = service._resolve_disk_for_slot(0, None, mapping, disks, {}, {}, candidate,
                                                    parse_ssh_outputs({}, 1, None))
        view = service._build_slot_view(
            0, 0, 0, {}, candidate, resolution.disk, mapping, parse_ssh_outputs({}, 1, None), {}, set(),
            resolution_source=resolution.source, stale_manual_mapping=resolution.stale_manual_mapping,
        )
        return resolution, view

    def test_candidate_keeps_unknown_presence(self) -> None:
        for name, outputs in UNKNOWN_INPUTS.items():
            with self.subTest(name):
                candidate = _candidate(outputs)
                # Unknown occupancy: no boolean and no source (an absent key
                # means the same thing as None to every consumer).
                self.assertIsNone(candidate.get("present"))
                self.assertIsNone(candidate.get("presence_source"))
                # The candidate still carries SES evidence for the bay.
                self.assertEqual(candidate["ses_device"], "/dev/sg0")

    def test_candidate_keeps_decided_presence(self) -> None:
        # Controls: positive and explicit-absence evidence keep their booleans.
        self.assertIs(_candidate(_ec("OK"))["present"], True)
        for name, outputs in EMPTY_INPUTS.items():
            with self.subTest(name):
                self.assertIs(_candidate(outputs)["present"], False)

    def test_unknown_presence_keeps_a_valid_manual_mapping(self) -> None:
        service = self.service()
        mapping = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
        for name, outputs in UNKNOWN_INPUTS.items():
            if name == "ec-critical":
                continue  # A Critical condition marks the mapped disk faulty; covered below.
            with self.subTest(name):
                disk = _disk()
                candidate = _candidate(outputs)
                self.assertFalse(service._slot_has_live_empty_evidence(candidate))
                resolution, view = self.resolve(service, candidate, mapping, disk)
                self.assertEqual(resolution.source, "manual")
                self.assertIs(resolution.disk, disk)
                self.assertFalse(resolution.stale_manual_mapping)
                self.assertEqual(view.state.value, "healthy")
                self.assertTrue(view.present)
                self.assertEqual(view.serial, "SANITIZED-MAPPED")

    def test_explicit_empty_still_overrides_a_manual_mapping(self) -> None:
        service = self.service()
        mapping = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
        for name, outputs in EMPTY_INPUTS.items():
            with self.subTest(name):
                resolution, view = self.resolve(service, _candidate(outputs), mapping, _disk())
                self.assertEqual(resolution.source, "ses-empty")
                self.assertIsNone(resolution.disk)
                self.assertTrue(resolution.stale_manual_mapping)
                self.assertEqual(view.state.value, "empty")
                self.assertFalse(view.present)

    def test_unknown_presence_without_a_disk_is_not_reported_empty(self) -> None:
        service = self.service()
        for name, outputs in UNKNOWN_INPUTS.items():
            with self.subTest(name):
                resolution, view = self.resolve(service, _candidate(outputs), None, None)
                self.assertNotEqual(resolution.source, "ses-empty")
                self.assertIsNone(resolution.disk)
                # Neither occupancy nor a disk is known. A condition code on
                # its own (some shelves latch Critical onto every empty bay)
                # is not a drive fault either.
                self.assertEqual(view.state.value, "unknown")
                self.assertFalse(view.present)
                self.assertEqual(view.non_identify_state, "unknown")

    def test_unknown_presence_with_mapping_but_no_disk_is_unmapped_not_stale(self) -> None:
        service = self.service()
        mapping = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
        resolution, view = self.resolve(service, _candidate(UNKNOWN_INPUTS["truncated-aes"]), mapping, None)
        self.assertNotEqual(resolution.source, "ses-empty")
        self.assertFalse(resolution.stale_manual_mapping)
        self.assertEqual(view.state.value, "unmapped")

    def test_condition_code_on_a_resolved_disk_still_reports_fault(self) -> None:
        # Control: once a disk is resolved, a Critical condition is a fault.
        service = self.service()
        mapping = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
        resolution, view = self.resolve(service, _candidate(UNKNOWN_INPUTS["ec-critical"]), mapping, _disk())
        self.assertEqual(resolution.source, "manual")
        self.assertEqual(view.state.value, "fault")


SHOW_HEADER = (
    "ses0:  <ExampleCo SynthShelf 0001>; ID: synthetic-shelf\n"
    "Desc  Device  Model  Serial  Status\n"
)


def _show_candidate(row: str) -> dict:
    return _candidate({"sesutil show": SHOW_HEADER + row + "\n"})


class CoreSesutilShowPresenceTests(unittest.TestCase):
    """A missing device name in `sesutil show` is not a report of an empty bay."""

    resolve = UnknownSesPresenceTests.resolve

    def service(self) -> InventoryService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        system = SystemConfig(id="core-show-presence", truenas=TrueNASConfig(platform="core"),
                              ssh=SSHConfig(enabled=False))
        return InventoryService(
            settings, system, AsyncMock(), AsyncMock(), None,
            MappingStore(str(Path(directory.name) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(directory.name) / "slot_detail_cache.json")),
        )

    def test_show_row_without_a_device_name_leaves_presence_unknown(self) -> None:
        for status in ("Unknown", "OK", "Critical"):
            with self.subTest(status=status):
                row = f"Slot 00  -  SANITIZED-MODEL  SANITIZED-MAPPED  {status}"
                slot = parse_sesutil_show_enclosures(SHOW_HEADER + row)[0].slots[0]
                self.assertIsNone(slot.present)
                self.assertIsNone(slot.presence_source)
                candidate = _show_candidate(row)
                self.assertIsNone(candidate.get("present"))
                self.assertIsNone(candidate.get("presence_source"))

    def test_show_row_without_a_device_name_keeps_a_valid_mapping(self) -> None:
        service = self.service()
        mapping = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
        candidate = _show_candidate("Slot 00  -  SANITIZED-MODEL  SANITIZED-MAPPED  Unknown")
        self.assertFalse(service._slot_has_live_empty_evidence(candidate))
        disk = _disk()
        resolution, view = self.resolve(service, candidate, mapping, disk)
        self.assertEqual(resolution.source, "manual")
        self.assertIs(resolution.disk, disk)
        self.assertFalse(resolution.stale_manual_mapping)
        self.assertEqual(view.state.value, "healthy")

    def test_show_row_without_a_device_name_or_disk_is_unknown(self) -> None:
        service = self.service()
        candidate = _show_candidate("Slot 00  -  -  -  Unknown")
        resolution, view = self.resolve(service, candidate, None, None)
        self.assertNotEqual(resolution.source, "ses-empty")
        self.assertEqual(view.state.value, "unknown")

    def test_show_named_device_and_not_installed_controls(self) -> None:
        service = self.service()
        mapping = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
        named = _show_candidate("Slot 00  da0  SANITIZED-MODEL  SANITIZED-MAPPED  Unknown")
        self.assertIs(named["present"], True)
        self.assertEqual(named["presence_source"], "sesutil_show")
        resolution, view = self.resolve(service, named, mapping, _disk())
        self.assertEqual(resolution.source, "manual")
        self.assertEqual(view.state.value, "healthy")
        # Control: the explicit absence report still wins over a stored mapping.
        empty = _show_candidate("Slot 00  -  -  -  Not installed")
        self.assertIs(empty["present"], False)
        self.assertEqual(empty["presence_source"], "sesutil_show")
        resolution, view = self.resolve(service, empty, mapping, _disk())
        self.assertEqual(resolution.source, "ses-empty")
        self.assertTrue(resolution.stale_manual_mapping)
        self.assertEqual(view.state.value, "empty")


if __name__ == "__main__":
    unittest.main()
