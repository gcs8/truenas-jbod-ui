"""Capacity is a reading, not free-form enclosure text."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from app.config import Settings, StorageViewConfig, SystemConfig, TrueNASConfig
from app.models.domain import EnclosureOption, InventorySnapshot
from app.route_support import templates
from app.services.parsers import (
    ParsedSSHData,
    build_slot_candidates_from_ses_enclosures,
    format_bytes,
    parse_sesutil_show_enclosures,
)
from app.services.public_demo_fixture import (
    build_public_demo_snapshot_bundle,
    build_static_demo_request,
    load_public_demo_fixture,
)
from app.services.profile_registry import SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID
from app.services.sas_fabric import _slot_storage_identity
from app.services.snapshot_export import SnapshotExportService
from app.services.supermicro_bmc import BMCDriveRecord, BMCInventory
from tests.inventory_fixtures import build_inventory_service


DESCRIPTOR = "SANITIZED-DESCRIPTOR 4T"


class SlotCapacityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = build_inventory_service(
            Settings(), SystemConfig(id="synthetic", truenas=TrueNASConfig(platform="core")),
            AsyncMock(), AsyncMock(), directory.name,
        )

    def slot(self, reported_size: object = DESCRIPTOR, disk_size: int | None = None):
        disk = None
        if disk_size is not None:
            disk = self.service._build_disk_records(
                [{"name": "da0", "serial": "SANITIZED-CAPACITY", "size": disk_size}],
                ParsedSSHData(), {}, {},
            )[0]
        return self.service._build_slot_view(
            0, 0, 0, {"id": "synthetic-shelf"},
            {"present": True, "ses_device": "/dev/ses0", "reported_size": reported_size,
             "serial_hint": "SANITIZED-CAPACITY"},
            disk, None, ParsedSSHData(), {}, set(),
        )

    def snapshot(self, slot):
        return InventorySnapshot(
            slots=[slot], selected_enclosure_id="synthetic-shelf",
            layout_rows=[[0]], layout_slot_count=1, refresh_interval_seconds=30,
        )

    def test_core_ses_parser_to_slot_never_treats_descriptor_as_capacity(self):
        enclosures = parse_sesutil_show_enclosures(
            "ses0:  <Example Shelf 0001>; ID: synthetic-shelf\n"
            "Desc  Device  Model  Serial  Status\n"
            f"Slot 00  da0  Example Disk  SANITIZED-CAPACITY  {DESCRIPTOR}, OK\n"
        )
        candidates, meta = build_slot_candidates_from_ses_enclosures(enclosures, 1, None)
        self.assertEqual(candidates[0]["reported_size"], DESCRIPTOR)
        slot = self.service._build_slot_view(
            0, 0, 0, meta, candidates[0], None, None, ParsedSSHData(), {}, set(),
        )
        self.assertTrue(slot.present)
        self.assertIsNone(slot.size_bytes)
        self.assertIsNone(slot.size_human)
        self.assertEqual(slot.raw_status["reported_size"], DESCRIPTOR)

    def test_only_standalone_positive_numeric_capacity_is_admitted(self):
        for text, expected in (("4T", 4_000_000_000_000), ("4 TB", 4_000_000_000_000),
                               ("3.5 TiB", 3_848_290_697_216), ("4096", 4096), (4096, 4096)):
            with self.subTest(text=text):
                slot = self.slot(text)
                self.assertEqual(slot.size_bytes, expected)
                self.assertEqual(slot.size_human, format_bytes(expected))
        for text in (DESCRIPTOR, "4T trailing", "prefix 4T", "4 TB (estimated)", "4iB", "0", "-4T",
                     "NaN", "Infinity", "", None, True, False, 0, -1, "9" * 400):
            with self.subTest(text=text):
                slot = self.slot(text)
                self.assertIsNone(slot.size_bytes)
                self.assertIsNone(slot.size_human)

    def test_inventory_capacity_takes_precedence_over_descriptor_or_numeric_hint(self):
        for text in (DESCRIPTOR, "8T", None):
            with self.subTest(text=text):
                slot = self.slot(text, disk_size=4_000_000_000_000)
                self.assertEqual(slot.size_bytes, 4_000_000_000_000)
                self.assertEqual(slot.size_human, format_bytes(slot.size_bytes))

    def test_missing_inventory_capacity_can_use_a_numeric_reading(self):
        slot = self.slot("4T", disk_size=0)
        self.assertEqual(slot.size_bytes, 4_000_000_000_000)
        self.assertEqual(slot.size_human, format_bytes(slot.size_bytes))

    def test_legacy_cached_descriptor_cannot_restore_a_capacity(self):
        cached = self.slot(None)
        entry = self.service._build_slot_detail_entry(cached, smart_summary=None, loaded_entries={})
        self.assertIsNotNone(entry)
        entry.slot_fields["size_human"] = DESCRIPTOR
        slot = self.slot(None)
        self.service._apply_persisted_slot_details([slot], loaded_entries={"synthetic:synthetic-shelf:0": entry})
        self.assertIsNone(slot.size_bytes)
        self.assertIsNone(slot.size_human)

    def test_legacy_cached_capacity_remains_available(self):
        cached = self.slot(None)
        entry = self.service._build_slot_detail_entry(cached, smart_summary=None, loaded_entries={})
        entry.slot_fields["size_human"] = "4 TB"
        slot = self.slot(None)
        self.service._apply_persisted_slot_details([slot], loaded_entries={"synthetic:synthetic-shelf:0": entry})
        self.assertEqual(slot.size_human, "4 TB")

    def test_esxi_bmc_only_disk_preserves_its_structured_capacity(self):
        self.service.system.truenas.platform = "esxi"
        self.service.system.default_profile_id = SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID
        bmc = BMCInventory(drives=[BMCDriveRecord(
            controller_id=0, physical_index=0, slot_number=2, enclosure_id="252",
            serial="SANITIZED-BMC-CAPACITY", size_bytes=4_000_000_000_000, health="ONLINE",
        )])
        slots, *_ = self.service._correlate_esxi_host(ParsedSSHData(), [], None, bmc)
        disk = next(slot for slot in slots if slot.present)
        self.assertEqual(disk.serial, "SANITIZED-BMC-CAPACITY")
        self.assertEqual(disk.size_bytes, 4_000_000_000_000)
        self.assertEqual(disk.size_human, format_bytes(disk.size_bytes))

    def test_serial_matched_bmc_capacity_is_not_lost_behind_ses_descriptor(self):
        drive = self.service._build_disk_records(
            [{"name": "da0", "serial": "SANITIZED-CAPACITY", "size": 4_000_000_000_000}],
            ParsedSSHData(), {}, {},
        )[0]
        for text in (None, DESCRIPTOR):
            with self.subTest(text=text):
                raw = {"reported_size": text, "present": True}
                self.service._apply_bmc_serial_match_to_raw_slot_status(raw, drive, None)
                slot = self.service._build_slot_view(
                    0, 0, 0, {"id": "synthetic-shelf"}, raw,
                    None, None, ParsedSSHData(), {}, set(),
                )
                self.assertEqual(slot.size_bytes, drive.size_bytes)
                self.assertEqual(slot.size_human, format_bytes(drive.size_bytes))

    def test_linux_vendor_capacity_preserves_the_numeric_producer_reading(self):
        ssh = ParsedSSHData(ubntstorage_disks=[{
            "slot": 1, "node": "sda", "size": 4_000_000_000_000, "state": "ONLINE",
        }])
        candidates = self.service._build_linux_vendor_slot_candidates(
            ssh, EnclosureOption(id="synthetic-shelf", label="Synthetic shelf"),
        )
        raw = next(iter(candidates.values()))
        slot = self.service._build_slot_view(
            0, 0, 0, {"id": "synthetic-shelf"}, raw,
            None, None, ParsedSSHData(), {}, set(),
        )
        self.assertEqual(slot.size_bytes, 4_000_000_000_000)
        self.assertEqual(slot.size_human, format_bytes(slot.size_bytes))

    def test_cached_bytes_regenerate_capacity_instead_of_restoring_descriptor(self):
        entry = self.service._build_slot_detail_entry(self.slot(None), smart_summary=None, loaded_entries={})
        entry.slot_fields.update(size_bytes=4_000_000_000_000, size_human=DESCRIPTOR)
        slot = self.slot(None)
        self.service._apply_persisted_slot_details([slot], loaded_entries={"synthetic:synthetic-shelf:0": entry})
        self.assertEqual(slot.size_bytes, 4_000_000_000_000)
        self.assertEqual(slot.size_human, format_bytes(slot.size_bytes))

    def test_invalid_cached_bytes_do_not_supply_a_capacity(self):
        for value in (True, 0, -1, DESCRIPTOR):
            with self.subTest(value=value):
                entry = self.service._build_slot_detail_entry(self.slot(None), smart_summary=None, loaded_entries={})
                entry.slot_fields.update(size_bytes=value, size_human=DESCRIPTOR)
                slot = self.slot(None)
                self.service._apply_persisted_slot_details([slot], loaded_entries={"synthetic:synthetic-shelf:0": entry})
                self.assertIsNone(slot.size_bytes)
                self.assertIsNone(slot.size_human)

    def test_public_demo_keeps_missing_capacity_unknown_and_real_readings_intact(self):
        fixture = load_public_demo_fixture()
        unread = next(slot for slot in fixture.slots if slot.size_bytes)
        unread.size_bytes = None
        unread.size_human = None
        bundle = build_public_demo_snapshot_bundle(fixture=fixture)
        slots = {slot.slot: slot for slot in bundle.primary_snapshot.slots}
        self.assertIsNone(slots[unread.slot].size_bytes)
        self.assertIsNone(slots[unread.slot].size_human)
        control = next(slot for slot in fixture.slots if slot.size_bytes)
        self.assertEqual(slots[control.slot].size_bytes, control.size_bytes)
        self.assertEqual(slots[control.slot].size_human, control.size_human)

    def test_storage_view_and_fabric_identity_share_the_qualified_capacity(self):
        for text, expected in ((DESCRIPTOR, None), ("4T", 4_000_000_000_000)):
            with self.subTest(text=text):
                slot = self.slot(text)
                view = self.service._build_ses_storage_view_runtime(
                    StorageViewConfig(id="synthetic-view", label="Synthetic view", kind="ses_enclosure",
                                      template_id="ses-auto"),
                    self.snapshot(slot),
                )
                runtime_slot = view.slots[0]
                converted = self.service._build_slot_view_from_storage_view_runtime_slot(view, runtime_slot)
                for consumer in (runtime_slot, converted):
                    self.assertEqual(consumer.size_bytes, expected)
                    self.assertEqual(consumer.size_human, format_bytes(expected))
                identity = _slot_storage_identity(slot)
                self.assertEqual(identity.get("size_bytes"), expected)
                self.assertEqual(identity.get("size_human"), format_bytes(expected))

    async def test_offline_export_embeds_unknown_capacity_and_retains_descriptor(self):
        exporter = SnapshotExportService(Settings(), AsyncMock(configured=False), templates)
        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_static_demo_request(), snapshot=self.snapshot(self.slot()),
            smart_summary_cache={}, selected_slot=0, history_window_hours=24,
            history_panel_open=False, io_chart_mode="total",
        )
        payload, _ = json.JSONDecoder().raw_decode(rendered.html.split("    snapshot: ", 1)[1])
        slot = payload["slots"][0]
        self.assertIsNone(slot.get("size_bytes"))
        self.assertIsNone(slot.get("size_human"))
        self.assertEqual(slot["raw_status"]["reported_size"], DESCRIPTOR)
