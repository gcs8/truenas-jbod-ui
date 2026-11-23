"""SES helpers keep the service's inherited methods and replacement seams."""

from __future__ import annotations

import inspect
import re
import unittest
from unittest.mock import patch

from app.services import inventory, inventory_sources
from app.services.inventory import InventoryService
from app.services.parsers import ParsedSSHData


MOVED_SES_METHODS = {
    "_admit_sg_ses_devices": staticmethod,
    "_split_sg_ses_discovery_output": staticmethod,
    "_parse_sg_ses_discovery_devices": classmethod,
    "_build_sg_ses_discovery_command": staticmethod,
    "_tag_ses_overlay": staticmethod,
    "_augment_ses_targets_from_redundant_hosts": staticmethod,
    "_merge_ses_overlay_data": staticmethod,
}


class InventorySESHelperSplitTests(unittest.TestCase):
    def test_helpers_have_one_inherited_definition(self) -> None:
        mixin = inventory_sources.InventorySourceCollectionMixin
        service = object.__new__(InventoryService)
        for name, kind in MOVED_SES_METHODS.items():
            with self.subTest(name=name):
                self.assertNotIn(name, InventoryService.__dict__)
                descriptor = mixin.__dict__[name]
                self.assertIsInstance(descriptor, kind)
                self.assertIs(inspect.getattr_static(InventoryService, name), descriptor)
                if kind is classmethod:
                    self.assertIs(getattr(service, name).__self__, InventoryService)
                else:
                    self.assertIs(getattr(service, name), descriptor.__func__)

    def test_discovery_still_reads_inventory_limits_and_patterns(self) -> None:
        values = ["/dev/sg4", "/dev/sg2", "/dev/sg1", "ignored"]
        self.assertEqual(InventoryService._admit_sg_ses_devices(values),
                         (["/dev/sg1", "/dev/sg2", "/dev/sg4"], 1, 0))
        with patch.object(inventory, "SG_SES_MAX_DISCOVERED_DEVICES", 1):
            self.assertEqual(InventoryService._admit_sg_ses_devices(values), (["/dev/sg1"], 1, 2))
        with patch.object(inventory, "SG_SES_DEVICE_RE", re.compile(r"/dev/sg(2)")):
            self.assertEqual(InventoryService._admit_sg_ses_devices(values), (["/dev/sg2"], 3, 0))
        with patch.object(inventory, "SG_SES_DISCOVERY_SKIPPED_RE", re.compile(r"omitted (\d+)")):
            self.assertEqual(InventoryService._split_sg_ses_discovery_output("omitted 7\n/dev/sg2"),
                             (["/dev/sg2"], 7))

    def test_discovery_classmethod_still_dispatches_on_the_calling_class(self) -> None:
        class DerivedInventory(InventoryService):
            @staticmethod
            def _split_sg_ses_discovery_output(stdout: str) -> tuple[list[str], int]:
                return [stdout], 0

        with patch.object(DerivedInventory, "_admit_sg_ses_devices", return_value=(["sentinel"], 0, 0)) as admit:
            self.assertEqual(DerivedInventory._parse_sg_ses_discovery_devices("synthetic"), ["sentinel"])
        admit.assert_called_once_with(["synthetic"])

    def test_discovery_command_still_reads_inventory_configuration(self) -> None:
        with patch.object(inventory, "SG_SES_NON_ENCLOSURE_TYPES", ("synthetic-type",)):
            command = InventoryService._build_sg_ses_discovery_command()
        self.assertIn('case "$type" in synthetic-type)', command)
        with patch.object(inventory.shlex, "join", return_value="synthetic command"):
            self.assertEqual(InventoryService._build_sg_ses_discovery_command(), "synthetic command")

    def test_tagging_still_reads_inventory_helpers(self) -> None:
        overlay = ParsedSSHData(ses_slot_candidates={1: {"ses_targets": [{"ses_device": "/dev/sg1"}]}})
        replacement = [{"synthetic": True}]
        with patch.object(inventory, "normalize_text", return_value="tagged-host"), \
                patch.object(inventory, "_merge_control_targets", return_value=replacement) as merge:
            InventoryService._tag_ses_overlay(overlay, "original-host")
        self.assertIs(overlay.ses_slot_candidates[1]["ses_targets"], replacement)
        merge.assert_called_once_with([{"ses_device": "/dev/sg1", "ssh_host": "tagged-host"}], [])

    def test_overlay_merges_still_read_inventory_helpers(self) -> None:
        base, overlay = ParsedSSHData(), ParsedSSHData()
        candidates, metadata, enclosures = {3: {"synthetic": True}}, {"synthetic": True}, []
        with patch.object(inventory, "merge_slot_candidate_maps", return_value=candidates) as slots, \
                patch.object(inventory, "merge_enclosure_meta", return_value=metadata) as meta, \
                patch.object(inventory, "_merge_ses_enclosures", return_value=enclosures) as shelves:
            merged = InventoryService._merge_ses_overlay_data(base, overlay)
            self.assertIs(merged.ses_slot_candidates, candidates)
            self.assertIs(merged.ses_selected_meta, metadata)
            self.assertIs(merged.ses_enclosures, enclosures)
            slots.assert_called_once_with(base.ses_slot_candidates, overlay.ses_slot_candidates)
            meta.assert_called_once_with(base.ses_selected_meta, overlay.ses_selected_meta)
            shelves.assert_called_once_with([])
        with patch.object(inventory, "_merge_ses_enclosures", return_value=enclosures) as shelves:
            self.assertIs(InventoryService._augment_ses_targets_from_redundant_hosts(base, [base, overlay]), base)
            self.assertIs(base.ses_enclosures, enclosures)
        shelves.assert_called_once_with([])
