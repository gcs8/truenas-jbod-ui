"""Only a 32-bit loginfo value may receive a vendor-reference classification.

A loginfo token wider than 32 bits must not be decoded from its low 32 bits.
It stays bounded raw evidence with an unconfirmed result.
"""

from __future__ import annotations

import unittest

from app.services.sas_diagnostics.lsi_loginfo import decode_lsi_loginfo
from app.services.sas_fabric import _diagnostic_metric_summary
from app.services.sas_fabric_parsers import parse_mpr_dmesg_events


KNOWN_LABEL = "LSI SAS IOP Boot: Invalid SAS address"


def _pipeline_row(loginfo: str) -> dict:
    events = parse_mpr_dmesg_events(
        "mpr0: Controller reported scsi ioc terminated tgt 1 SMID 1 loginfo " + loginfo
    )
    rows = events["by_controller"]["mpr0"]["event_table"]["rows"]
    if len(rows) != 1:
        raise AssertionError(f"expected one event row, got {len(rows)}")
    return rows[0]


class LoginfoWidthTests(unittest.TestCase):
    def assert_rejected(self, row: dict, raw: str) -> None:
        for source in (row, row["decoded"]):
            self.assertNotEqual(source.get("label"), KNOWN_LABEL)
            self.assertEqual(source.get("decode_confidence"), "unconfirmed")
            self.assertIn("32 bits", source.get("decoder_note", ""))
            self.assertNotIn("vendor", source)
            self.assertNotIn("lsi_loginfo", source)
        self.assertEqual(row["decoded"]["loginfo"], raw)

    def test_eight_digit_control_keeps_vendor_reference(self) -> None:
        row = _pipeline_row("30010000")

        self.assertEqual(row["label"], KNOWN_LABEL)
        self.assertEqual(row["decode_confidence"], "vendor-reference")

    def test_nine_digit_value_with_known_suffix_is_unconfirmed(self) -> None:
        self.assert_rejected(_pipeline_row("130010000"), "130010000")

    def test_leading_zero_over_width_token_is_unconfirmed(self) -> None:
        self.assert_rejected(_pipeline_row("030010000"), "030010000")

    def test_boundary_values(self) -> None:
        for raw, admitted in (
            ("0", True),
            ("ffffffff", True),
            ("0xffffffff", True),
            ("100000000", False),
            ("0x100000000", False),
        ):
            with self.subTest(raw=raw):
                decoded = decode_lsi_loginfo(raw)
                assert decoded is not None
                if admitted:
                    self.assertIn("lsi_loginfo", decoded)
                    self.assertNotIn("32 bits", decoded.get("decoder_note", ""))
                else:
                    self.assertNotIn("lsi_loginfo", decoded)
                    self.assertEqual(decoded["decode_confidence"], "unconfirmed")
                    self.assertIn("32 bits", decoded["decoder_note"])

    def test_rejected_raw_evidence_is_bounded(self) -> None:
        raw = "3" + "0" * 200
        decoded = decode_lsi_loginfo(raw)
        assert decoded is not None

        self.assertEqual(decoded["decode_confidence"], "unconfirmed")
        self.assertLessEqual(len(decoded["loginfo"]), 32)
        self.assertTrue(decoded["loginfo"].endswith("..."))
        row = _pipeline_row(raw)
        self.assertLessEqual(len(row["decoded"]["loginfo"]), 32)


class LoginfoSummaryBoundTests(unittest.TestCase):
    def test_rejected_loginfo_never_becomes_a_summary_key(self) -> None:
        wide = "3" + "0" * 4095
        lines = [
            "mpr0: Controller reported scsi ioc terminated tgt 1 SMID 1 loginfo " + wide,
            "mpr0: Controller reported scsi ioc terminated tgt 1 SMID 2 loginfo 130010000",
            "mpr0: Controller reported scsi ioc terminated tgt 1 SMID 3 loginfo 30010000",
        ]
        events = parse_mpr_dmesg_events("\n".join(lines))

        for scope, key in (("by_controller", "mpr0"), ("by_controller_target", "mpr0:1")):
            summary = events[scope][key]
            with self.subTest(scope=scope):
                self.assertEqual(summary["loginfo_counts"], {"30010000": 1})
                self.assertEqual(summary.get("loginfo_rejected_count"), 2)
                for key in summary["loginfo_counts"]:
                    self.assertLessEqual(len(key), 10)
                self.assertEqual(summary["ioc_terminated_count"], 3)
                # The per-disk and per-controller metric view carries the count.
                metric = _diagnostic_metric_summary(summary)
                self.assertEqual(metric["loginfo_rejected_count"], 2)
                self.assertEqual(metric["loginfo_counts"], {"30010000": 1})

    def test_admitted_loginfo_keeps_its_count_and_no_rejections(self) -> None:
        events = parse_mpr_dmesg_events(
            "mpr0: Controller reported scsi ioc terminated tgt 1 SMID 1 loginfo 31120302"
        )
        summary = events["by_controller"]["mpr0"]

        self.assertEqual(summary["loginfo_counts"], {"31120302": 1})
        self.assertEqual(summary.get("loginfo_rejected_count"), 0)


# A loginfo field whose value is not a hex number of at most 32 bits, or that
# is followed by more text (including a second loginfo tail), is malformed.
MALFORMED_LOGINFO = (
    "3001zz00",
    "+30010000",
    "-1",
    "3001_0000",
    "\u06630010000",
    "gg",
    "30010000,1",
    "30010000 extra",
    "zz tgt 1 SMID 2 loginfo 30010000",
    "",
)


class MalformedLoginfoTests(unittest.TestCase):
    """The loginfo field's complete raw value is read; a rejected one stays visible."""

    SCOPES = (("by_controller", "mpr0"), ("by_controller_target", "mpr0:1"))

    @staticmethod
    def _line(loginfo: str) -> str:
        return f"mpr0: Controller reported scsi ioc terminated tgt 1 SMID 1 loginfo {loginfo}".rstrip()

    def assert_valid_control(self) -> None:
        events = parse_mpr_dmesg_events(self._line("30010000"))
        row = events["by_controller"]["mpr0"]["event_table"]["rows"][0]
        self.assertEqual(row["label"], KNOWN_LABEL)
        self.assertEqual(row["decode_confidence"], "vendor-reference")
        for scope, key in self.SCOPES:
            self.assertEqual(events[scope][key]["loginfo_counts"], {"30010000": 1})
            self.assertEqual(events[scope][key]["loginfo_rejected_count"], 0)

    def test_malformed_values_are_rejected_not_absent(self) -> None:
        for raw in MALFORMED_LOGINFO:
            with self.subTest(loginfo=raw):
                events = parse_mpr_dmesg_events(self._line(raw))
                row = events["by_controller"]["mpr0"]["event_table"]["rows"][0]
                for source in (row, row["decoded"]):
                    self.assertNotEqual(source.get("label"), KNOWN_LABEL)
                    self.assertEqual(source.get("family"), "controller_terminated_io")
                    self.assertEqual(source.get("decode_confidence"), "unconfirmed")
                    self.assertIn("not decoded", source.get("decoder_note", ""))
                    self.assertNotIn("vendor", source)
                    self.assertNotIn("lsi_loginfo", source)
                self.assertEqual(row["decoded"]["loginfo"], raw.lower())
                self.assertEqual(row["loginfo"], raw)
                self.assertEqual(row["target"], "1")
                self.assertEqual(row["smid"], "1")
                for scope, key in self.SCOPES:
                    summary = events[scope][key]
                    self.assertEqual(summary["loginfo_counts"], {})
                    self.assertEqual(summary["loginfo_rejected_count"], 1)
                    metric = _diagnostic_metric_summary(summary)
                    self.assertEqual(metric["loginfo_counts"], {})
                    self.assertEqual(metric["loginfo_rejected_count"], 1)
                self.assert_valid_control()

    def test_rejected_raw_evidence_is_bounded(self) -> None:
        raw = "z" * 200
        events = parse_mpr_dmesg_events(self._line(raw))
        row = events["by_controller"]["mpr0"]["event_table"]["rows"][0]

        self.assertIsInstance(row["decoded"].get("loginfo"), str)
        self.assertLessEqual(len(row["decoded"]["loginfo"]), 32)
        self.assertTrue(row["decoded"]["loginfo"].endswith("..."))
        self.assertLessEqual(len(row["loginfo"]), 384)


class KernelTextSummaryKeyTests(unittest.TestCase):
    """Free kernel text is bounded like row text before it becomes a summary key."""

    SCOPES = (("by_controller", "mpr0"), ("by_device", "da0"), ("by_controller_target", "mpr0:1"))

    def test_long_kernel_text_is_bounded_before_it_becomes_a_summary_key(self) -> None:
        reason = "r" * 5000
        status = "s" * 5000
        events = parse_mpr_dmesg_events(
            f"(da0:mpr0:0:1:0): SCSI sense: ABORTED COMMAND asc:4b,04 ({reason})\n"
            f"(da0:mpr0:0:1:0): CAM status: {status}\n"
        )
        for scope, key in self.SCOPES:
            with self.subTest(scope=scope):
                summary = events[scope][key]
                # The metric view republishes sense_counts only.
                for view, fields in (
                    (summary, (("sense_counts", reason), ("cam_status_counts", status))),
                    (_diagnostic_metric_summary(summary), (("sense_counts", reason),)),
                ):
                    for field, text in fields:
                        counts = view[field]
                        self.assertEqual(sum(counts.values()), 1)
                        (only_key,) = counts
                        self.assertLessEqual(len(only_key), 384)
                        self.assertTrue(only_key.startswith(text[:64]))
                        self.assertTrue(only_key.endswith("..."))

    def test_short_kernel_text_keeps_its_exact_key(self) -> None:
        events = parse_mpr_dmesg_events(
            "(da0:mpr0:0:1:0): SCSI sense: ABORTED COMMAND asc:4b,04 (synthetic reason)\n"
            "(da0:mpr0:0:1:0): CAM status: SCSI Status Error\n"
        )
        summary = events["by_controller"]["mpr0"]

        self.assertEqual(summary["sense_counts"], {"synthetic reason": 1})
        self.assertEqual(summary["cam_status_counts"], {"SCSI Status Error": 1})


if __name__ == "__main__":
    unittest.main()
