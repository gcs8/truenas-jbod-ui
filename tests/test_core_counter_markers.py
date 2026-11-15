"""Malformed PHY markers in CORE expander counter output (#1220).

The counter read command prints one ``@phy=NN`` marker per requested PHY.
Output that repeats a marker, carries an invalid one, or repeats the header is
ambiguous: whichever frame the parser kept would decide the reading. The whole
chunk must be refused so every PHY reads ``unknown`` / ``rejected_output``,
never a measured zero or an order-dependent error count. Synthetic output only.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.core_expander_counters import (
    COUNTER_FIELDS,
    SMP_PHY_ERROR_LOG_MARKER,
    SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX,
    chunk_stop,
    counter_summary,
    parse_chunk_output,
    PhyErrorLogResult,
    parse_phy_error_log_output,
)
from app.services.sas_fabric import _merged_phy_error_log_rows

# Built at run time so the source carries no literal 16-digit address.
EXPANDER = "5" + "0" * 14 + "2"
OTHER_EXPANDER = "5" + "0" * 14 + "3"
DEVICE = "pass10"
IDENTITY = f"0:{EXPANDER}:{DEVICE}"


def _frame(phy: int, invalid_dwords: int = 0) -> str:
    """A valid REPORT PHY ERROR LOG response for ``phy``, as hex words."""
    data = bytearray(28)
    data[:4] = bytes((0x41, 0x11, 0x00, 0x06))
    data[9] = phy
    data[12:16] = invalid_dwords.to_bytes(4, "big")
    return " ".join(f"{byte:02x}" for byte in data)


def _key(first: int, last: int) -> str:
    return f"{SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX}{IDENTITY}:{first:02x}-{last:02x}"


def _header(first: int, last: int, identity: str = IDENTITY) -> str:
    return f"{SMP_PHY_ERROR_LOG_MARKER}:{identity}:{first:02x}-{last:02x}"


def _output(first: int, last: int, body: list[str]) -> str:
    return "\n".join([_header(first, last), *body, "@end"]) + "\n"


def _chunk(key: str, stdout: str) -> PhyErrorLogResult:
    parsed = parse_chunk_output(key, stdout)
    assert parsed is not None, "a counter key always parses"
    return parsed


class MalformedPhyMarkerTests(unittest.TestCase):
    def assert_rejected(self, key: str, stdout: str, phys: list[int]) -> None:
        parsed = _chunk(key, stdout)
        self.assertTrue(parsed.rejected, "malformed markers must refuse the whole chunk")
        self.assertEqual([row["phy"] for row in parsed.rows], phys)
        for row in parsed.rows:
            self.assertEqual((row["counter_state"], row["counter_reason"]), ("unknown", "rejected_output"))
            self.assertTrue(all(row[field] is None for field in COUNTER_FIELDS))
        self.assertEqual(
            counter_summary(parsed.rows), {"measured_phys": 0, "unknown_phys": len(phys), "phys_with_errors": 0},
        )
        self.assertIsNone(parsed.capability)
        self.assertEqual(chunk_stop(key, True, stdout), ("rejected_output", phys[0]))

    def test_repeated_phy_marker_rejects_the_chunk_in_either_frame_order(self):
        for counts in ((0, 99), (99, 0), (0, 0)):
            body = [line for count in counts for line in ("@phy=00", _frame(0, count))]
            with self.subTest(counts=counts):
                self.assert_rejected(_key(0, 0), _output(0, 0, body), [0])

    def test_repeated_phy_marker_inside_a_multi_phy_chunk_rejects_every_phy(self):
        body = ["@phy=00", _frame(0), "@phy=01", _frame(1), "@phy=00", _frame(0, 7), "@phy=02", _frame(2)]
        self.assert_rejected(_key(0, 2), _output(0, 2, body), [0, 1, 2])

    def test_invalid_ff_marker_rejects_the_chunk(self):
        body = ["@phy=00", _frame(0), "@phy=ff", _frame(0, 99)]
        self.assert_rejected(_key(0, 0), _output(0, 0, body), [0])

    def test_marker_that_is_not_two_lowercase_hex_digits_rejects_the_chunk(self):
        for marker in ("@phy=FF", "@phy=0", "@phy=001", "@phy=", "@phy=zz"):
            body = ["@phy=00", _frame(0), marker, _frame(0, 99), "@phy=01", _frame(1)]
            with self.subTest(marker=marker):
                self.assert_rejected(_key(0, 1), _output(0, 1, body), [0, 1])

    def test_repeated_or_invalid_header_rejects_the_chunk(self):
        for extra in (
            _header(0, 0),
            _header(0, 0, f"0:{OTHER_EXPANDER}:{DEVICE}"),
            f"{SMP_PHY_ERROR_LOG_MARKER}:not-a-header",
        ):
            stdout = "\n".join([_header(0, 0), extra, "@phy=00", _frame(0), "@end"]) + "\n"
            with self.subTest(extra=extra):
                self.assert_rejected(_key(0, 0), stdout, [0])

    def test_marker_outside_the_requested_range_rejects_the_chunk(self):
        body = ["@phy=00", _frame(0), "@phy=05", _frame(5)]
        self.assert_rejected(_key(0, 1), _output(0, 1, body), [0, 1])

    def test_parser_reports_marker_errors_instead_of_dropping_them(self):
        clean = parse_phy_error_log_output(_output(0, 0, ["@phy=00", _frame(0)]))
        self.assertFalse(clean.malformed)
        for body in (
            ["@phy=00", _frame(0), "@phy=00", _frame(0, 99)],
            ["@phy=00", _frame(0), "@phy=ff", _frame(0, 99)],
        ):
            with self.subTest(body=body):
                self.assertTrue(parse_phy_error_log_output(_output(0, 0, body)).malformed)

    def test_fabric_projection_reads_a_repeated_marker_as_unknown(self):
        key = _key(0, 0)
        routed = {(0, EXPANDER): SimpleNamespace(device=DEVICE)}
        for counts in ((0, 99), (99, 0)):
            body = [line for count in counts for line in ("@phy=00", _frame(0, count))]
            with self.subTest(counts=counts):
                rows = _merged_phy_error_log_rows({key: _output(0, 0, body)}, routed, {})
                row = rows[(0, EXPANDER)][0]
                self.assertEqual((row["counter_state"], row["counter_reason"]), ("unknown", "rejected_output"))
                self.assertIsNone(row["invalid_dword_count"])


class WellFormedOutputControls(unittest.TestCase):
    def test_single_zero_frame_is_a_measured_clean_reading(self):
        parsed = _chunk(_key(0, 0), _output(0, 0, ["@phy=00", _frame(0)]))
        self.assertFalse(parsed.rejected)
        self.assertEqual(parsed.rows[0]["counter_state"], "measured")
        self.assertEqual(parsed.rows[0]["invalid_dword_count"], 0)
        self.assertEqual(counter_summary(parsed.rows), {"measured_phys": 1, "unknown_phys": 0, "phys_with_errors": 0})
        self.assertIsNone(chunk_stop(_key(0, 0), True, _output(0, 0, ["@phy=00", _frame(0)])))

    def test_single_nonzero_frame_is_a_measured_error_reading(self):
        parsed = _chunk(_key(0, 0), _output(0, 0, ["@phy=00", _frame(0, 99)]))
        self.assertFalse(parsed.rejected)
        self.assertEqual(parsed.rows[0]["invalid_dword_count"], 99)
        self.assertEqual(counter_summary(parsed.rows), {"measured_phys": 1, "unknown_phys": 0, "phys_with_errors": 1})

    def test_multi_phy_chunk_and_no_response_stay_accepted(self):
        body = ["@phy=00", _frame(0), "@phy=01", _frame(1, 3), "@phy=02", _frame(2)]
        parsed = _chunk(_key(0, 2), _output(0, 2, body))
        self.assertFalse(parsed.rejected)
        self.assertEqual([row["counter_state"] for row in parsed.rows], ["measured"] * 3)
        hung = _output(0, 2, ["@phy=00", _frame(0), "@phy=01", "@no-response"])
        parsed = _chunk(_key(0, 2), hung)
        self.assertFalse(parsed.rejected)
        self.assertEqual([row["counter_reason"] for row in parsed.rows], [None, "no_response"])
        self.assertEqual(chunk_stop(_key(0, 2), True, hung), ("no_response", 1))

    def test_highest_valid_phy_marker_is_accepted(self):
        parsed = _chunk(_key(254, 254), _output(254, 254, ["@phy=fe", _frame(254)]))
        self.assertFalse(parsed.rejected)
        self.assertEqual(parsed.rows[0]["phy"], 254)


if __name__ == "__main__":
    unittest.main()
