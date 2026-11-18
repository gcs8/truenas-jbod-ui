"""A drive row whose speed column is not a number keeps its enclosure and slot.

mprutil prints a device's negotiated speed as a number (`1.5`, `3.0`, `6.0`,
`12`) or, for a rate it has no name for, as `Unk` or `???`. A row with such a
speed is read with the columns after it in their places: the enclosure handle
and slot are never taken from the wrong fields. A row whose columns after the
device type do not read as speed, enclosure handle and slot is not understood,
so the device list is not read whole and the enclosure's device count is
withheld instead of published short (#1400).

StorCLI replies are read as JSON objects keyed by column name (`Sp`,
`EID:Slt`), so a non-numeric speed there cannot move the enclosure and slot;
a control below pins that.

Synthetic replies only.
"""

from __future__ import annotations

import json
import unittest

from app.services.parsers import parse_storcli_physical_drives, storcli_physical_drive_list_complete
from app.services.sas_fabric_parsers import parse_mpr_device_list
from tests.test_core_fabric_legacy_controller import (
    DEVICE_ADDRESS,
    DEVICES_HEADING,
    SELECTED_ENCLOSURE,
    _core_fabric,
    _healthy_unit,
    _inventory_slot,
    _printed_device_row,
    _show_all,
)

# A second end device in the same enclosure (handle 0010), slot 01.
SECOND_ADDRESS = f"5{4:015x}"


def _row(speed: str, *, device: str = "SAS Target", slot: str = "01") -> str:
    return f"00   11    {SECOND_ADDRESS} 000c    000a      {device:<13} {speed:<5} 0010 {slot:<5} 1\n"


def _listing(*rows: str) -> str:
    return DEVICES_HEADING + _printed_device_row() + "".join(rows) + "\n"


def _fabric(outputs: dict[str, str]):
    return _core_fabric(outputs, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)


def _enclosure_metrics(fabric) -> list[dict]:
    return [node.metrics for node in fabric.nodes if node.kind == "mpr-enclosure"]


class MprUnknownSpeedRowTests(unittest.TestCase):
    """A printed `Unk` or `???` speed does not move the enclosure and slot columns."""

    def test_unknown_speed_row_keeps_its_enclosure_and_slot(self) -> None:
        for speed in ("Unk", "???"):
            with self.subTest(speed=speed):
                rows, rejected = parse_mpr_device_list(_listing(_row(speed)))
                self.assertEqual(rejected, 0)
                self.assertEqual(
                    [(row["sas_address"], row["device"], row["speed"], row["enclosure_handle"], row["slot"]) for row in rows],
                    [
                        (DEVICE_ADDRESS, "SAS Target", "12", "0010", "00"),
                        (SECOND_ADDRESS, "SAS Target", speed, "0010", "01"),
                    ],
                )

    def test_unknown_speed_row_is_counted_in_its_own_enclosure(self) -> None:
        for speed in ("Unk", "???"):
            listing = _listing(_row(speed))
            for label, outputs in {
                "dedicated": {**_healthy_unit(), "mprutil -u 0 show devices": listing},
                "show all": {"mprutil -u 0 show all": _show_all().replace(_printed_device_row(), _printed_device_row() + _row(speed))},
            }.items():
                with self.subTest(speed=speed, source=label):
                    fabric = _fabric(outputs)
                    self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "read")
                    [enclosure] = _enclosure_metrics(fabric)
                    self.assertEqual(enclosure["device_count"], 2)
                    self.assertNotIn("not_read", enclosure)

    def test_columns_that_do_not_read_as_speed_enclosure_and_slot_are_not_understood(self) -> None:
        # A speed word mprutil does not print, and a row with no speed column:
        # either way the enclosure handle would come from another column.
        rows = {
            "unprinted speed word": _row("Fast"),
            "no speed column": f"00   11    {SECOND_ADDRESS} 000c    000a      SAS Target    0010 01    1\n",
            "slot not a number": _row("12", slot="xx"),
        }
        for name, bad_row in rows.items():
            with self.subTest(name, layer="parser"):
                parsed, rejected = parse_mpr_device_list(_listing(bad_row))
                self.assertEqual(rejected, 1)
                self.assertEqual([row["sas_address"] for row in parsed], [DEVICE_ADDRESS])
            with self.subTest(name, layer="fabric"):
                fabric = _fabric({**_healthy_unit(), "mprutil -u 0 show devices": _listing(bad_row)})
                self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "unreadable")
                [enclosure] = _enclosure_metrics(fabric)
                self.assertEqual((enclosure["device_count"], enclosure.get("not_read")), (None, ["device_count"]))

    def test_a_row_missing_a_field_is_not_understood_even_with_a_four_digit_slot(self) -> None:
        # A four-digit slot (up to 1023 on one expander) looks like an
        # enclosure handle; a row missing its handle, speed or slot must still
        # be refused by its column count, never read with a shifted handle.
        rows = {
            "enclosure handle missing": f"00   7     {SECOND_ADDRESS} 000c    000b      SAS Target    Unk   1023  1\n",
            "speed missing": f"00   7     {SECOND_ADDRESS} 000c    000b      SAS Target    0010 1023  1\n",
            "slot missing": f"00   7     {SECOND_ADDRESS} 000c    000b      SAS Target    Unk   0010 1\n",
            "width missing": f"00   7     {SECOND_ADDRESS} 000c    000b      SAS Target    ???   0010 1023\n",
        }
        for name, bad_row in rows.items():
            with self.subTest(name, layer="parser"):
                parsed, rejected = parse_mpr_device_list(_listing(bad_row))
                self.assertEqual(rejected, 1)
                self.assertEqual([row["sas_address"] for row in parsed], [DEVICE_ADDRESS])
            for label, outputs in {
                "dedicated": {**_healthy_unit(), "mprutil -u 0 show devices": _listing(bad_row)},
                "show all": {"mprutil -u 0 show all": _show_all().replace(_printed_device_row(), _printed_device_row() + bad_row)},
            }.items():
                with self.subTest(name, layer="fabric", source=label):
                    fabric = _fabric(outputs)
                    self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "unreadable")
                    [enclosure] = _enclosure_metrics(fabric)
                    self.assertEqual((enclosure["device_count"], enclosure.get("not_read")), (None, ["device_count"]))

    def test_bare_rows_without_the_heading_are_no_looser_than_before(self) -> None:
        # Rows without mprutil's heading carry no width column, so their
        # layout cannot show a missing field; such a row with a speed that is
        # not a number stays not understood, as before (the missing-handle row).
        bare = f"{SECOND_ADDRESS} 000c 000b SAS Target"
        for name, row in {
            "enclosure handle missing": f"00 7 {bare} Unk 1023 1",
            "unknown speed": f"{bare} ??? 0010 01",
            "speed missing": f"{bare} 0010 1023 1",
        }.items():
            with self.subTest(name):
                self.assertEqual(parse_mpr_device_list(row + "\n"), ([], 1))
        # Control: the bare numeric form keeps reading.
        rows, rejected = parse_mpr_device_list(f"{bare} 12.0 0010 1023\n")
        self.assertEqual(rejected, 0)
        self.assertEqual([(row["speed"], row["enclosure_handle"], row["slot"]) for row in rows], [("12.0", "0010", "1023")])

    def test_a_complete_row_at_a_four_digit_slot_is_read_in_place(self) -> None:
        # Control: the whole printed layout at slot 1023 keeps its own columns.
        for speed in ("Unk", "???", "12"):
            row = _row(speed, slot="1023")
            with self.subTest(speed=speed, layer="parser"):
                rows, rejected = parse_mpr_device_list(_listing(row))
                self.assertEqual(rejected, 0)
                self.assertEqual(
                    (rows[1]["speed"], rows[1]["enclosure_handle"], rows[1]["slot"]),
                    (speed, "0010", "1023"),
                )
            for label, outputs in {
                "dedicated": {**_healthy_unit(), "mprutil -u 0 show devices": _listing(row)},
                "show all": {"mprutil -u 0 show all": _show_all().replace(_printed_device_row(), _printed_device_row() + row)},
            }.items():
                with self.subTest(speed=speed, layer="fabric", source=label):
                    fabric = _fabric(outputs)
                    self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "read")
                    [enclosure] = _enclosure_metrics(fabric)
                    self.assertEqual(enclosure["device_count"], 2)
                    self.assertNotIn("not_read", enclosure)

    def test_numeric_speeds_and_a_named_disk_are_unchanged(self) -> None:
        # Controls: every numeric speed mprutil prints, and a disk name after the type.
        for speed in ("1.5", "3.0", "6.0", "12"):
            with self.subTest(speed=speed):
                rows, rejected = parse_mpr_device_list(_listing(_row(speed)))
                self.assertEqual(rejected, 0)
                self.assertEqual((rows[1]["speed"], rows[1]["enclosure_handle"], rows[1]["slot"]), (speed, "0010", "01"))
        rows, rejected = parse_mpr_device_list(_listing(_row("12", device="SAS Target da5")))
        self.assertEqual(rejected, 0)
        self.assertEqual(
            (rows[1]["device"], rows[1]["speed"], rows[1]["enclosure_handle"], rows[1]["slot"]),
            ("SAS Target da5", "12", "0010", "01"),
        )
        fabric = _fabric({**_healthy_unit(), "mprutil -u 0 show devices": _listing(_row("12"))})
        self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "read")
        self.assertEqual([metrics["device_count"] for metrics in _enclosure_metrics(fabric)], [2])

    def test_every_numeric_speed_form_read_before_is_still_read_in_place(self) -> None:
        # The numeric speed grammar is unchanged (`\d+(?:\.\d+)?`): any run of
        # digits, with or without a fraction, including a leading zero and
        # three or more digits. Each complete row keeps its own enclosure and slot.
        speeds = ("1", "12", "100", "1000", "012.0", "100.0", "1.5", "22.5", "0.75")
        bare = f"{SECOND_ADDRESS} 000c 000b SAS Target"
        for speed in speeds:
            with self.subTest(speed=speed, layout="heading"):
                rows, rejected = parse_mpr_device_list(_listing(_row(speed, slot="1023")))
                self.assertEqual(rejected, 0)
                self.assertEqual((rows[1]["speed"], rows[1]["enclosure_handle"], rows[1]["slot"]), (speed, "0010", "1023"))
            with self.subTest(speed=speed, layout="bare with width"):
                rows, rejected = parse_mpr_device_list(f"00 7 {bare} {speed} 0010 1023 1\n")
                self.assertEqual(rejected, 0)
                self.assertEqual([(row["speed"], row["enclosure_handle"], row["slot"]) for row in rows], [(speed, "0010", "1023")])
            for label, outputs in {
                "dedicated": {**_healthy_unit(), "mprutil -u 0 show devices": _listing(_row(speed, slot="1023"))},
                "show all": {
                    "mprutil -u 0 show all": _show_all().replace(
                        _printed_device_row(), _printed_device_row() + _row(speed, slot="1023")
                    )
                },
            }.items():
                with self.subTest(speed=speed, layer="fabric", source=label):
                    fabric = _fabric(outputs)
                    self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "read")
                    self.assertEqual([metrics["device_count"] for metrics in _enclosure_metrics(fabric)], [2])
            # Bare without the width: every form but a four-digit whole number,
            # which reads equally as handle, slot and width with the speed
            # missing; that one row is refused, never guessed.
            with self.subTest(speed=speed, layout="bare without width"):
                parsed = parse_mpr_device_list(f"{bare} {speed} 0010 1023\n")
                if speed == "1000":
                    self.assertEqual(parsed, ([], 1))
                else:
                    self.assertEqual(parsed[1], 0)
                    self.assertEqual([(r["speed"], r["enclosure_handle"], r["slot"]) for r in parsed[0]], [(speed, "0010", "1023")])
        # The missing-enclosure row stays refused, under the heading and bare.
        missing = f"00   7     {SECOND_ADDRESS} 000c    000b      SAS Target    Unk   1023  1\n"
        self.assertEqual(parse_mpr_device_list(_listing(missing))[1], 1)
        self.assertEqual(parse_mpr_device_list(f"00 7 {bare} Unk 1023 1\n"), ([], 1))

    def test_a_complete_row_with_an_invalid_enclosure_handle_is_not_understood(self) -> None:
        # Every column is present, but the enclosure handle is not four hex
        # digits: the row is refused, not read under a handle no enclosure has.
        for handle in ("zzzz", "00g0", "010", "00100"):
            bad_row = f"00   11    {SECOND_ADDRESS} 000c    000a      SAS Target    12    {handle} 01    1\n"
            with self.subTest(handle=handle, layer="parser"):
                parsed, rejected = parse_mpr_device_list(_listing(bad_row))
                self.assertEqual(rejected, 1)
                self.assertEqual([row["sas_address"] for row in parsed], [DEVICE_ADDRESS])
            with self.subTest(handle=handle, layer="bare"):
                self.assertEqual(parse_mpr_device_list(f"{SECOND_ADDRESS} 000c 000b SAS Target 12 {handle} 01 1\n"), ([], 1))
            for label, outputs in {
                "dedicated": {**_healthy_unit(), "mprutil -u 0 show devices": _listing(bad_row)},
                "show all": {"mprutil -u 0 show all": _show_all().replace(_printed_device_row(), _printed_device_row() + bad_row)},
            }.items():
                with self.subTest(handle=handle, layer="fabric", source=label):
                    fabric = _fabric(outputs)
                    self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "unreadable")
                    [enclosure] = _enclosure_metrics(fabric)
                    self.assertEqual((enclosure["device_count"], enclosure.get("not_read")), (None, ["device_count"]))
        # Control: the same row with a valid handle is read and counted.
        good_row = f"00   11    {SECOND_ADDRESS} 000c    000a      SAS Target    12    0010 01    1\n"
        rows, rejected = parse_mpr_device_list(_listing(good_row))
        self.assertEqual((rejected, rows[1]["enclosure_handle"], rows[1]["slot"]), (0, "0010", "01"))
        fabric = _fabric({**_healthy_unit(), "mprutil -u 0 show devices": _listing(good_row)})
        self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "read")
        self.assertEqual([metrics["device_count"] for metrics in _enclosure_metrics(fabric)], [2])


class StorcliUnknownSpeedControlTests(unittest.TestCase):
    """Control: a StorCLI row is keyed by column name, so `Unk` in `Sp` moves nothing."""

    def test_unknown_speed_keeps_enclosure_and_slot(self) -> None:
        rows = [
            {"EID:Slt": "13:0", "DID": 0, "State": "Onln", "Sp": "U", "Model": "Synthetic", "SN": "SYNTH-0"},
            {"EID:Slt": "13:1", "DID": 1, "State": "Onln", "Sp": "Unk", "Model": "Synthetic", "SN": "SYNTH-1"},
        ]
        reply = json.dumps(
            {"Controllers": [{"Command Status": {"Status": "Success"}, "Response Data": {"Drive Information": rows}}]}
        )
        drives = parse_storcli_physical_drives(reply, [], "c0")
        self.assertEqual(
            [(drive["enclosure_id"], drive["slot"], drive["slot_key"], drive["serial"]) for drive in drives],
            [("13", 0, "13:0", "SYNTH-0"), ("13", 1, "13:1", "SYNTH-1")],
        )
        self.assertTrue(storcli_physical_drive_list_complete(reply))


if __name__ == "__main__":
    unittest.main()
