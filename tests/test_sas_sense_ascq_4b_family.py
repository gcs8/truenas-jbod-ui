"""Sense data with ASC 0x4B is classified by its ASC/ASCQ, never by the text.

``_sense_fault_family`` returns from its ``asc_code == 0x4B`` branch for every
ASCQ, so the reason and sense-key text never decide the family of an ASC 0x4B
event. These tests pin that path for every ASCQ, including the three pairs
(0x4B/0x03, 0x4B/0x04, 0x4B/0x07) a later fallback used to repeat.
"""

from __future__ import annotations

import unittest

from app.services.sas_diagnostics import scsi
from app.services.sas_diagnostics.scsi import decode_scsi_sense_event

# Text that would select a different family if the text arms ever decided.
MISLEADING_REASONS = ("", "NAK received", "connection lost", "timeout", "bus reset occurred", "aborted")


def _expected_4b_family(ascq: int) -> str:
    if ascq in {0x03, 0x06, 0x0F}:
        return "timeout"
    if ascq == 0x07:
        return "link_loss"
    if 0x08 <= ascq <= 0x0D:
        return "data_buffer_error"
    if 0x0E <= ascq <= 0x15:
        return "pcie_fabric"
    return "sas_protocol"


class SenseAsc4bFamilyTests(unittest.TestCase):
    def test_repeated_pairs_keep_their_family(self) -> None:
        for asc, family in (((0x4B, 0x03), "timeout"), ((0x4B, 0x04), "sas_protocol"), ((0x4B, 0x07), "link_loss")):
            for reason in MISLEADING_REASONS:
                with self.subTest(asc=asc, reason=reason):
                    self.assertEqual(scsi._sense_fault_family(reason, "ABORTED COMMAND", asc), family)

    def test_every_ascq_is_decided_by_the_asc_branch(self) -> None:
        for ascq in range(0x100):
            expected = _expected_4b_family(ascq)
            for reason in MISLEADING_REASONS:
                with self.subTest(ascq=ascq, reason=reason):
                    self.assertEqual(scsi._sense_fault_family(reason, "", (0x4B, ascq)), expected)

    def test_decoded_event_uses_the_asc_family(self) -> None:
        for asc_text, family in (("4b,3", "timeout"), ("4b,4", "sas_protocol"), ("4b,7", "link_loss")):
            with self.subTest(asc=asc_text):
                decoded = decode_scsi_sense_event(
                    {"reason": "connection lost", "sense_key": "ABORTED COMMAND", "asc": asc_text}
                )
                self.assertEqual(decoded["family"], family)

    def test_text_still_decides_without_an_asc(self) -> None:
        # Control: with no ASC the text arms are the live path.
        for reason, family in (
            ("NAK received", "sas_protocol"),
            ("connection lost", "link_loss"),
            ("timeout", "timeout"),
            ("bus reset occurred", "bus_reset"),
            ("aborted", "aborted_command"),
            ("", "scsi_sense"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(scsi._sense_fault_family(reason, "", None), family)


if __name__ == "__main__":
    unittest.main()
