"""SES status flags are read from the whole field value only (#1275).

``sg_ses`` prints binary element flags as ``Field=0`` or ``Field=1`` separated
by commas. A reader that checks only the first character after ``=`` turns a
malformed value such as ``10`` or ``1x`` into a definite flag. These tests feed
synthetic ``sg_ses`` text through the enclosure-status (``-p ec``) reader and
the joined status reader and require that only a whole ``0`` or ``1`` sets a
flag; any other value leaves it unknown (``None``). No host is contacted.
"""

from __future__ import annotations

import unittest

from app.services.parsers import (
    parse_sg_ses_enclosure_status,
    parse_sg_ses_join_filter,
)

MALFORMED_VALUES = ("10", "00", "1x", "0x", "-1", "1.", "0.", "", "01", "1 x")
STATUS_FIELDS = (
    ("Predicted failure", "predicted_failure"),
    ("Disabled", "disabled"),
    ("Swap", "swapped"),
    ("Hot spare", "hot_spare"),
)
CONTROL_FIELDS = (
    ("Do not remove", "do_not_remove"),
    ("Fault sensed", "fault_sensed"),
    ("Fault reqstd", "fault_requested"),
)


def _status_line(values: dict[str, str]) -> str:
    # The shared status-line helper reads every status flag from the one line
    # that starts with ``Predicted failure=`` and carries ``status:``.
    fields = ", ".join(
        f"{name}={values.get(name, '0')}" for name, _attribute in STATUS_FIELDS
    )
    return fields + ", status: OK\n"


def _control_lines(values: dict[str, str]) -> str:
    # Laid out as sg_ses prints an Array device slot element's control bits.
    return (
        f"App client bypass A=0, Do not remove={values.get('Do not remove', '0')}, "
        "Enc bypass A=0, Enc bypass B=0\n"
        "Ready to insert=0, RMV=0, Ident=0, Report=0\n"
        f"App client bypass B=0, Fault sensed={values.get('Fault sensed', '0')}, "
        f"Fault reqstd={values.get('Fault reqstd', '0')}, Device off=0\n"
    )


def _ec_slot(body: str):
    enclosure = parse_sg_ses_enclosure_status(
        "Element type: Array device slot, subenclosure id: 0\n"
        "Element 0 descriptor:\n" + body
    )
    return enclosure.slots[0]


def _joined_slot(body: str):
    enclosure = parse_sg_ses_join_filter(
        "Slot00 [0,0] Element type: Array device slot\n" + body + "device slot number: 0\n"
    )
    return enclosure.slots[0]


READERS = (("ec", _ec_slot), ("join", _joined_slot))


class StatusLineFlagWholeValueTests(unittest.TestCase):
    def test_whole_zero_and_one_set_each_status_flag_in_both_readers(self) -> None:
        for reader_name, read in READERS:
            for field_name, attribute in STATUS_FIELDS:
                for value, expected in (("1", True), ("0", False)):
                    with self.subTest(reader=reader_name, field=field_name, value=value):
                        slot = read(_status_line({field_name: value}))
                        self.assertIs(getattr(slot, attribute), expected)

    def test_malformed_status_flag_values_stay_unknown_in_both_readers(self) -> None:
        for reader_name, read in READERS:
            for field_name, attribute in STATUS_FIELDS:
                for value in MALFORMED_VALUES:
                    with self.subTest(reader=reader_name, field=field_name, value=value):
                        slot = read(_status_line({field_name: value}))
                        self.assertIsNone(getattr(slot, attribute))
                        # A malformed neighbour does not disturb the valid fields.
                        for other_name, other_attribute in STATUS_FIELDS:
                            if other_name != field_name:
                                self.assertIs(getattr(slot, other_attribute), False)

    def test_issue_example_values_are_unknown_not_shortened(self) -> None:
        line = "Predicted failure=10, Disabled=00, Swap=1x, status: OK\n"
        for reader_name, read in READERS:
            with self.subTest(reader=reader_name):
                slot = read(line)
                self.assertEqual(
                    (slot.predicted_failure, slot.disabled, slot.swapped),
                    (None, None, None),
                )


class ElementControlFlagWholeValueTests(unittest.TestCase):
    def test_whole_zero_and_one_set_each_control_flag(self) -> None:
        for field_name, attribute in CONTROL_FIELDS:
            for value, expected in (("1", True), ("0", False)):
                with self.subTest(field=field_name, value=value):
                    slot = _ec_slot(_status_line({}) + _control_lines({field_name: value}))
                    self.assertIs(getattr(slot, attribute), expected)

    def test_malformed_control_flag_values_stay_unknown(self) -> None:
        for field_name, attribute in CONTROL_FIELDS:
            for value in MALFORMED_VALUES:
                with self.subTest(field=field_name, value=value):
                    slot = _ec_slot(_status_line({}) + _control_lines({field_name: value}))
                    self.assertIsNone(getattr(slot, attribute))
                    for other_name, other_attribute in CONTROL_FIELDS:
                        if other_name != field_name:
                            self.assertIs(getattr(slot, other_attribute), False)


if __name__ == "__main__":
    unittest.main()
