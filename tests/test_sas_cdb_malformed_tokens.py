"""A malformed CDB byte token must never become a different, valid command.

The kernel prints a CDB as space-separated two-digit hexadecimal bytes. When a
token in that run is not a two-digit hex byte, the decoder must keep the input
identifiable as malformed: it must not drop the token and shift later bytes into
earlier field positions, and it must not derive address or length fields from
bytes it cannot place.
"""

from __future__ import annotations

import unittest

from app.services.sas_diagnostics.scsi import decode_scsi_cdb_message
from app.services.sas_fabric_parsers import parse_mpr_dmesg_events


VALID_READ10 = "READ(10). CDB: 28 00 00 00 00 00 00 00 01 00"
VALID_READ10_HEX = "28 00 00 00 00 00 00 00 01 00"
NUMERIC_FIELDS = (
    "lba",
    "transfer_blocks",
    "allocation_length",
    "parameter_list_length",
    "log_page_code",
)


def _pipeline_row(message: str) -> dict:
    events = parse_mpr_dmesg_events("(da0:mpr0:0:1:0): " + message)
    rows = events["by_controller"]["mpr0"]["event_table"]["rows"]
    if len(rows) != 1:
        raise AssertionError(f"expected one event row, got {len(rows)}")
    return rows[0]


class MalformedCdbTokenTests(unittest.TestCase):
    def assert_malformed(self, row: dict, raw_cdb: str) -> None:
        decoded = row["decoded"]
        for source in (row, decoded):
            # The malformed command must not read as the control command.
            self.assertNotEqual(source.get("cdb_hex"), VALID_READ10_HEX)
            self.assertNotIn("cdb_hex", source)
            self.assertNotIn("byte_count", source)
            for field in NUMERIC_FIELDS:
                self.assertNotIn(field, source)
            self.assertIs(source.get("cdb_malformed"), True)
            self.assertEqual(source.get("cdb_raw"), raw_cdb)
            self.assertNotEqual(source.get("decode_confidence"), "standard")
            self.assertIn("malformed", source.get("decoder_note", ""))
        # Opcode recognition stays separate from whole-CDB validity.
        self.assertEqual(decoded["opcode"], "0x28")
        self.assertEqual(decoded["operation"], "READ(10)")
        self.assertEqual(decoded["decode_confidence"], "standard-partial")

    def test_valid_read10_control_decodes_fields(self) -> None:
        row = _pipeline_row(VALID_READ10)

        self.assertEqual(row["cdb_hex"], VALID_READ10_HEX)
        self.assertEqual(row["byte_count"], 10)
        self.assertEqual(row["lba"], 0)
        self.assertEqual(row["transfer_blocks"], 1)
        self.assertEqual(row["decode_confidence"], "standard")
        self.assertNotIn("cdb_malformed", row)
        self.assertNotIn("cdb_raw", row)
        self.assertNotIn("decoder_note", row)

    def test_valid_cdb_followed_by_kernel_text_is_not_malformed(self) -> None:
        # Trailing kernel fields may carry two-digit decimal values that look
        # like hex bytes; they must not invalidate the CDB before them.
        for trailer in (
            "length 512 SMID 77 Aborting command",
            "length 16 SMID 77 Aborting command",
            "length 32 SMID 10 Command timeout",
            "length 64 SMID 99",
        ):
            with self.subTest(trailer=trailer):
                row = _pipeline_row(f"{VALID_READ10} {trailer}")

                self.assertEqual(row["cdb_hex"], VALID_READ10_HEX)
                self.assertEqual(row["byte_count"], 10)
                self.assertEqual(row["lba"], 0)
                self.assertEqual(row["transfer_blocks"], 1)
                self.assertEqual(row["decode_confidence"], "standard")
                self.assertNotIn("cdb_malformed", row)

    def test_three_digit_token_is_not_compacted_into_the_control(self) -> None:
        raw = "28 000 00 00 00 00 00 00 00 01 00"
        self.assert_malformed(_pipeline_row(f"READ(10). CDB: {raw}"), raw)

    def test_nonhex_token_inside_the_cdb_is_reported_malformed(self) -> None:
        raw = "28 gg 00 00 00 00 00 00 01 00"
        self.assert_malformed(_pipeline_row(f"READ(10). CDB: {raw}"), raw)

    def test_word_token_inside_the_byte_run_is_reported_malformed(self) -> None:
        for raw in (
            "28 ggg 00 00 00 00 00 00 01 00",
            "28 z9z 00 00 00 00 00 00 01 00",
        ):
            with self.subTest(raw=raw):
                self.assert_malformed(_pipeline_row(f"READ(10). CDB: {raw}"), raw)

    def test_single_digit_token_inside_the_cdb_is_reported_malformed(self) -> None:
        raw = "28 0 00 00 00 00 00 00 00 01 00"
        self.assert_malformed(_pipeline_row(f"READ(10). CDB: {raw}"), raw)

    def test_malformed_first_token_leaves_operation_from_the_kernel(self) -> None:
        decoded = decode_scsi_cdb_message("READ(10). CDB: 280 00 00 00 00 00 00 00 01 00")

        self.assertIs(decoded.get("cdb_malformed"), True)
        self.assertNotIn("opcode", decoded)
        self.assertNotIn("cdb_hex", decoded)
        self.assertEqual(decoded["operation"], "READ(10)")
        self.assertEqual(decoded["decode_confidence"], "unconfirmed")
        for field in NUMERIC_FIELDS:
            self.assertNotIn(field, decoded)

    def test_malformed_raw_evidence_is_bounded(self) -> None:
        raw = "28 000 " + " ".join(["00"] * 400)
        row = _pipeline_row(f"READ(10). CDB: {raw}")

        self.assertIs(row.get("cdb_malformed"), True)
        self.assertLessEqual(len(row.get("cdb_raw", "")), 384)
        self.assertLessEqual(len(row["decoded"].get("cdb_raw", "")), 384)
        self.assertNotIn("cdb_hex", row)

    def test_truncated_read10_keeps_bytes_and_withholds_fields(self) -> None:
        row = _pipeline_row("READ(10). CDB: 28 00 00 00")

        self.assertEqual(row["cdb_hex"], "28 00 00 00")
        self.assertEqual(row["byte_count"], 4)
        self.assertNotIn("lba", row)
        self.assertNotIn("transfer_blocks", row)
        self.assertNotIn("cdb_malformed", row)


if __name__ == "__main__":
    unittest.main()
