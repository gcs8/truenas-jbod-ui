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
