"""Zero SAS addresses never prove that two hosts see the same shelf (#1217).

Cross-host shelf matching without a logical enclosure ID relies on a bay's
disk SAS address and the attached expander address. The parsers store a zero
address as the normalized text ``"0"``, so an empty bay on one shelf and an
empty bay on an unrelated shelf both carry ``("0", "0")``. Those values say
nothing about which shelf a bay is in. These tests build synthetic shelves
only; no host is contacted.
"""

from __future__ import annotations

import unittest

from app.services.inventory import InventoryService
from app.services.parsers import (
    SESMapEnclosure,
    SESMapSlot,
    _ses_enclosures_share_identity,
    parse_ssh_outputs,
)

NODE_A = "node-a.example.test"
NODE_B = "node-b.example.test"


def _aes_page(occupied_address: str, *, zero_empty_bay: bool, attached_address: str | None = None) -> str:
    """One unkeyed shelf: bay 0 empty, bay 1 holding a disk."""

    attached = attached_address or occupied_address
    text = "Synthetic shelf\nElement type: Array device slot\n"
    text += "Element index: 0\ndevice slot number: 0\nSAS device type: no SAS device attached\n"
    if zero_empty_bay:
        text += "attached SAS address: 0x0\nSAS address: 0x0\n"
    text += "Element index: 1\ndevice slot number: 1\nSAS device type: end device\n"
    text += f"attached SAS address: {attached}\nSAS address: {occupied_address}\n"
    return text


def _overlay(host: str, device: str, occupied_address: str, *, zero_empty_bay: bool,
             attached_address: str | None = None):
    page = _aes_page(occupied_address, zero_empty_bay=zero_empty_bay, attached_address=attached_address)
    parsed = parse_ssh_outputs({f"sg_ses -p aes {device}": page}, 2, None)
    InventoryService._tag_ses_overlay(parsed, host)
    return parsed


def _slot(number: int, sas: str | None, attached: str | None, *, present: bool | None = True) -> SESMapSlot:
    return SESMapSlot(slot_number=number, element_id=number, ses_device="/dev/sg2",
                      slot_number_source="ses_device_slot_number", present=present,
                      sas_address=sas, attached_sas_address=attached)


def _enclosure(host: str, *slots: SESMapSlot) -> SESMapEnclosure:
    return SESMapEnclosure(ses_device="/dev/sg2", ssh_host=host,
                           slots={slot.slot_number: slot for slot in slots})


class ZeroAddressShelfIdentityTests(unittest.TestCase):
    def test_unrelated_shelves_with_zero_address_empty_bays_stay_separate(self) -> None:
        for zero_empty_bay in (False, True):
            with self.subTest(zero_empty_bay=zero_empty_bay):
                left = _overlay(NODE_A, "/dev/sg2", "0x5eeeee01", zero_empty_bay=zero_empty_bay)
                right = _overlay(NODE_B, "/dev/sg9", "0x5eeeee02", zero_empty_bay=zero_empty_bay)
                # The parser keeps the zero address as normalized "0".
                if zero_empty_bay:
                    self.assertEqual(left.ses_enclosures[0].slots[0].sas_address, "0")
                    self.assertEqual(left.ses_enclosures[0].slots[0].attached_sas_address, "0")
                self.assertFalse(
                    _ses_enclosures_share_identity(left.ses_enclosures[0], right.ses_enclosures[0])
                )

                merged = InventoryService._augment_ses_targets_from_redundant_hosts(left, [left, right])

                self.assertEqual(len(merged.ses_enclosures), 2)
                self.assertEqual(
                    [enclosure.slots[1].sas_address_conflict for enclosure in merged.ses_enclosures],
                    [False, False],
                )
                for bay in (0, 1):
                    self.assertEqual(
                        {target.get("ssh_host") for target in merged.ses_slot_candidates[bay]["ses_targets"]},
                        {NODE_A},
                    )

    def test_same_shelf_on_two_hosts_still_merges_on_nonzero_evidence(self) -> None:
        # Positive control: the occupied bay carries the same nonzero disk and
        # expander addresses on both hosts, so the redundant path is admitted
        # even though the empty bay reports zero addresses.
        left = _overlay(NODE_A, "/dev/sg2", "0x5eeeee01", zero_empty_bay=True,
                        attached_address="0x5eeeee10")
        right = _overlay(NODE_B, "/dev/sg9", "0x5eeeee01", zero_empty_bay=True,
                         attached_address="0x5eeeee10")

        merged = InventoryService._augment_ses_targets_from_redundant_hosts(left, [left, right])

        self.assertEqual(len(merged.ses_enclosures), 1)
        self.assertEqual(
            {target.get("ssh_host") for target in merged.ses_slot_candidates[1]["ses_targets"]},
            {NODE_A, NODE_B},
        )
        # The empty bay is not an anchor, but it is the same physical shelf, so
        # its redundant control path still joins once identity is proven.
        self.assertEqual(
            {target.get("ssh_host") for target in merged.ses_slot_candidates[0]["ses_targets"]},
            {NODE_A, NODE_B},
        )

    def test_zero_spellings_never_anchor_identity(self) -> None:
        # Hand-built slots may carry any spelling of zero; all of them are absence.
        for sas, attached in (("0", "0"), ("0x0", "0x0"), ("0x00000000", "0"),
                              ("0x5eeeee01", "0"), ("0", "0x5eeeee10"),
                              ("0x5eeeee01", "0x00")):
            with self.subTest(sas=sas, attached=attached):
                first = _enclosure(NODE_A, _slot(0, sas, attached))
                second = _enclosure(NODE_B, _slot(0, sas, attached))
                self.assertFalse(_ses_enclosures_share_identity(first, second))

    def test_address_spelling_does_not_block_a_genuine_match(self) -> None:
        # Control: the same nonzero addresses in two spellings are one identity.
        first = _enclosure(NODE_A, _slot(0, "0x5eeeee01", "0x5eeeee10"))
        second = _enclosure(NODE_B, _slot(0, "5eeeee01", "5eeeee10"))
        self.assertTrue(_ses_enclosures_share_identity(first, second))

    def test_empty_bay_is_not_a_shelf_identity_anchor(self) -> None:
        # A bay the enclosure reports empty can still print stale nonzero
        # addresses; it must not join two shelves.
        first = _enclosure(NODE_A, _slot(0, "0x5eeeee01", "0x5eeeee10", present=False))
        second = _enclosure(NODE_B, _slot(0, "0x5eeeee01", "0x5eeeee10", present=False))
        self.assertFalse(_ses_enclosures_share_identity(first, second))
        # Control: the same addresses on an occupied bay do join.
        first.slots[0].present = True
        second.slots[0].present = True
        self.assertTrue(_ses_enclosures_share_identity(first, second))


if __name__ == "__main__":
    unittest.main()
