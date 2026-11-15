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
        for source in (row, row["decoded"]):
            evidence = source.get("cdb_raw", "")
            self.assertTrue(evidence.startswith("28 000 00"))
            self.assertLessEqual(len(evidence), 128)
            self.assertTrue(evidence.endswith("..."))
        self.assertNotIn("cdb_hex", row)

    def test_truncated_read10_keeps_bytes_and_withholds_fields(self) -> None:
        row = _pipeline_row("READ(10). CDB: 28 00 00 00")

        self.assertEqual(row["cdb_hex"], "28 00 00 00")
        self.assertEqual(row["byte_count"], 4)
        self.assertNotIn("lba", row)
        self.assertNotIn("transfer_blocks", row)
        self.assertNotIn("cdb_malformed", row)


class CdbTrailerGrammarTests(unittest.TestCase):
    """The byte run ends cleanly only at the end of the text or at the kernel's
    ``length <decimal>`` trailer. Every other token ends it as malformed."""

    def assert_refused(self, row: dict, raw_cdb: str, fields: tuple[str, ...] = NUMERIC_FIELDS) -> None:
        decoded = row["decoded"]
        for source in (row, decoded):
            self.assertIs(source.get("cdb_malformed"), True)
            self.assertEqual(source.get("cdb_raw"), raw_cdb)
            self.assertNotIn("cdb_hex", source)
            self.assertNotIn("byte_count", source)
            self.assertNotEqual(source.get("decode_confidence"), "standard")
            self.assertIn("malformed", source.get("decoder_note", ""))
            for field in fields:
                self.assertNotIn(field, source)

    def test_valid_controls_still_decode(self) -> None:
        for message, cdb_hex in (
            ("INQUIRY. CDB: 12 00 00 00 24 00", "12 00 00 00 24 00"),
            ("INQUIRY. CDB: 12 00 00 00 24 00 ", "12 00 00 00 24 00"),
            ("LOG SENSE. CDB: 4d 00 58 00 00 00 00 00 40 00", "4d 00 58 00 00 00 00 00 40 00"),
            (f"{VALID_READ10} length 16 SMID 77", VALID_READ10_HEX),
            (
                f"{VALID_READ10} length 4096 SMID 1234 Command timeout on target 1(0x000a) 60000 set",
                VALID_READ10_HEX,
            ),
        ):
            with self.subTest(message=message):
                row = _pipeline_row(message)
                self.assertEqual(row["cdb_hex"], cdb_hex)
                self.assertEqual(row["decode_confidence"], "standard")
                self.assertNotIn("cdb_malformed", row)

    def test_short_invalid_last_token_is_malformed(self) -> None:
        raw = "12 00 00 00 24 gg"
        row = _pipeline_row(f"INQUIRY. CDB: {raw}")

        self.assert_refused(row, raw)
        self.assertEqual(row["decoded"]["opcode"], "0x12")
        self.assertEqual(row["decoded"]["decode_confidence"], "standard-partial")

    def test_short_invalid_penultimate_token_is_malformed_for_log_sense(self) -> None:
        raw = "4d 00 58 00 00 00 00 00 gg 00"
        row = _pipeline_row(f"LOG SENSE. CDB: {raw}")

        self.assert_refused(row, raw, NUMERIC_FIELDS + ("log_page_control", "log_page_control_label"))
        self.assertEqual(row["decoded"]["opcode"], "0x4d")

    def test_consecutive_invalid_tokens_are_malformed(self) -> None:
        for raw in (
            "28 00 00 00 00 00 00 00 01 zz yy",
            "28 gg hh 00 00 00 00 00 01 00",
            "28 00 00 00 00 00 00 00 01 00 zzz 00",
        ):
            with self.subTest(raw=raw):
                self.assert_refused(_pipeline_row(f"READ(10). CDB: {raw}"), raw)

    def test_word_that_is_not_the_kernel_trailer_is_malformed(self) -> None:
        for raw in (
            "28 00 00 00 00 00 00 00 01 00 xyz",
            "28 00 00 00 00 00 00 00 01 00 length",
            "28 00 00 00 00 00 00 00 01 00 length abc",
            "28 00 00 00 00 00 00 00 01 00 (bogus)",
        ):
            with self.subTest(raw=raw):
                self.assert_refused(_pipeline_row(f"READ(10). CDB: {raw}"), raw)

    def test_malformed_first_token_keeps_the_kernel_operation(self) -> None:
        raw = "gg 00 00 00 24 00"
        row = _pipeline_row(f"INQUIRY. CDB: {raw}")

        self.assert_refused(row, raw)
        self.assertNotIn("opcode", row["decoded"])
        self.assertEqual(row["decoded"]["operation"], "INQUIRY")
        self.assertEqual(row["decoded"]["decode_confidence"], "unconfirmed")

    def test_empty_byte_run_before_the_trailer_is_malformed(self) -> None:
        raw = "length 512 SMID 7"
        row = _pipeline_row(f"READ(10). CDB: {raw}")

        self.assert_refused(row, raw)
        self.assertEqual(row["decoded"]["operation"], "READ(10)")

    def test_service_action_before_a_malformed_token_is_kept(self) -> None:
        raw = "9e 10 00 00 00 00 00 00 00 00 00 00 00 20 gg 00"
        row = _pipeline_row(f"SERVICE ACTION IN(16). CDB: {raw}")

        self.assert_refused(row, raw)
        decoded = row["decoded"]
        self.assertEqual(decoded["opcode"], "0x9e")
        self.assertEqual(decoded["service_action"], "0x10")
        self.assertEqual(decoded["decode_confidence"], "standard-partial")


if __name__ == "__main__":
    unittest.main()
