"""Text that is not a whole, well-formed byte or number is never decoded as one.

The kernel prints ASC/ASCQ as ``asc:%x,%x``: each part is one or two ASCII
hexadecimal digits. A wider part (even with leading zeros), a sign, an
underscore, a prefix, spaces or non-ASCII digits make the value malformed. A
malformed value gets an explicit undecodable result: no T10 label, no fault
family and a malformed note, never the label of the number it resembles.

The same rule covers the CAM error number: ASCII digits only.
"""

from __future__ import annotations

import unittest

from app.services.sas_diagnostics.decoder import decode_mpr_dmesg_event
from app.services.sas_diagnostics.scsi import decode_scsi_sense_event, parse_asc_ascq
from app.services.sas_fabric_parsers import parse_mpr_dmesg_events


REASON = "synthetic reason"
SENSE_KEY = "ABORTED COMMAND"
NAK_LABEL = "NAK received"

# Forms the kernel prints, and what they mean.
ADMITTED_ASC = (
    ("4b,4", (0x4B, 0x04)),
    ("4b,04", (0x4B, 0x04)),
    ("4B,4", (0x4B, 0x04)),
    ("0,0", (0x00, 0x00)),
    ("ff,ff", (0xFF, 0xFF)),
)

# Each of these used to decode as a known code (mostly NAK received).
REJECTED_ASC = (
    "04b,04",
    "4b,004",
    "0" * 256 + "4b,04",
    "+4b,04",
    "4b,+4",
    "-0,00",
    "4_b,04",
    "4b,0_4",
    "0x4b,04",
    " 4b,04",
    "4b,04 ",
    "4b, 04",
    "\u0664b,04",
    "\uff14b,04",
    "4b,04,1",
    "4b,",
    ",04",
    ",",
    "4b",
    "gg,04",
)


def _sense_line(asc: str) -> str:
    return f"(da0:mpr0:0:1:0): SCSI sense: {SENSE_KEY} asc:{asc} ({REASON})"


def _parse_one(line: str) -> tuple[dict, dict]:
    events = parse_mpr_dmesg_events(line)
    summary = events["by_controller"]["mpr0"]
    rows = summary["event_table"]["rows"]
    if len(rows) != 1:
        raise AssertionError(f"expected one event row, got {len(rows)}")
    return rows[0], events


class AscAscqReaderTests(unittest.TestCase):
    def test_kernel_forms_are_admitted(self) -> None:
        for text, expected in ADMITTED_ASC:
            with self.subTest(asc=text):
                self.assertEqual(parse_asc_ascq(text.lower()), expected)

    def test_malformed_parts_are_refused(self) -> None:
        for text in REJECTED_ASC:
            with self.subTest(asc=text):
                self.assertIsNone(parse_asc_ascq(text.lower()))


class MalformedAscSenseTests(unittest.TestCase):
    def assert_undecodable(self, decoded: dict) -> None:
        self.assertIs(decoded.get("asc_malformed"), True)
        self.assertNotIn("asc_label", decoded)
        self.assertNotIn("label", decoded)
        self.assertNotIn("family", decoded)
        self.assertNotIn("likely_layer", decoded)
        self.assertEqual(decoded.get("decode_confidence"), "unconfirmed")
        self.assertEqual(decoded.get("decode_source"), "kernel_message")
        self.assertIn("malformed", decoded.get("decoder_note", ""))
        self.assertLessEqual(len(decoded.get("asc", "")), 32)

    def test_valid_control_decodes_in_the_direct_decoder(self) -> None:
        decoded = decode_scsi_sense_event({"asc": "4b,04", "reason": REASON, "sense_key": SENSE_KEY})

        self.assertEqual(decoded["asc_label"], NAK_LABEL)
        self.assertEqual(decoded["family"], "sas_protocol")
        self.assertEqual(decoded["decode_confidence"], "standard")
        self.assertNotIn("asc_malformed", decoded)

    def test_rejected_tokens_are_undecodable_in_the_direct_decoder(self) -> None:
        for text in REJECTED_ASC:
            with self.subTest(asc=text):
                decoded = decode_scsi_sense_event({"asc": text, "reason": REASON, "sense_key": SENSE_KEY})
                self.assert_undecodable(decoded)
                self.assertEqual(decoded["sense_key"], SENSE_KEY)

    def test_absent_asc_is_not_reported_malformed(self) -> None:
        decoded = decode_scsi_sense_event({"asc": "", "reason": REASON, "sense_key": SENSE_KEY})

        self.assertNotIn("asc_malformed", decoded)
        self.assertEqual(decoded["family"], "aborted_command")
        self.assertEqual(decoded["decode_confidence"], "observed")

    def test_valid_control_decodes_through_the_event_pipeline(self) -> None:
        for text in ("4b,4", "4b,04"):
            with self.subTest(asc=text):
                row, _events = _parse_one(_sense_line(text))
                self.assertEqual(row["asc_label"], NAK_LABEL)
                self.assertEqual(row["family"], "sas_protocol")
                self.assertEqual(row["decode_confidence"], "standard")
                self.assertNotIn("asc_malformed", row)

    def test_over_wide_tokens_are_undecodable_through_the_event_pipeline(self) -> None:
        # The kernel-line pattern admits any run of hex digits on each side.
        for text in ("04b,04", "4b,004", "0" * 256 + "4b,04"):
            with self.subTest(asc=text[-12:]):
                row, events = _parse_one(_sense_line(text))
                self.assert_undecodable(row["decoded"])
                self.assertIs(row.get("asc_malformed"), True)
                self.assertNotIn("asc_label", row)
                self.assertNotIn("family", row)
                self.assertEqual(row.get("decode_confidence"), "unconfirmed")
                self.assertIn("malformed", row.get("decoder_note", ""))
                # The title is the kernel's own reason text, not a guess.
                self.assertEqual(row["label"], REASON)
                summary = events["by_controller"]["mpr0"]
                self.assertEqual(summary["fault_family_counts"], {})
                self.assertEqual(summary["top_findings"], [])
                self.assertEqual(summary["sense_count"], 1)


class CamErrorNumberTests(unittest.TestCase):
    def test_ascii_errno_control_decodes(self) -> None:
        decoded = decode_mpr_dmesg_event({"event_type": "cam_error", "message": "Error 5, Retries exhausted"})

        self.assertEqual(decoded["cam_error_code"], 5)
        self.assertEqual(decoded["errno_name"], "EIO")

    def test_non_ascii_digits_are_not_an_errno(self) -> None:
        for code in ("\u0665", "\uff15", "\u0661\u0660"):
            with self.subTest(code=code):
                decoded = decode_mpr_dmesg_event(
                    {"event_type": "cam_error", "message": f"Error {code}, Retries exhausted"}
                )
                self.assertNotIn("cam_error_code", decoded)
                self.assertNotIn("errno_name", decoded)
                self.assertEqual(decoded["family"], "cam_error")


if __name__ == "__main__":
    unittest.main()
