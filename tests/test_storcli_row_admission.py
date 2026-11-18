"""StorCLI drive rows: which entries are read as drives, and what they supply.

A `storcli /cN/eall/sall show all J` reply can carry a summary table
(`Drive Information` or `PD LIST`) and per-drive `Drive /cN/eM/sK` entries.
A drive only a per-drive entry reports is still a drive (#1305), and the
census check still refuses the reply.

Synthetic replies only.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from app.services.parsers import parse_storcli_physical_drives, storcli_physical_drive_list_complete
# The module, not its test class: a TestCase imported here would be collected here too.
from tests import test_ses_unknown_occupancy_consumers as occupancy


def _reply(response_data: dict[str, Any]) -> str:
    return json.dumps({"Controllers": [{"Command Status": {"Status": "Success"}, "Response Data": response_data}]})


def _row(slot: int, serial: str) -> dict[str, Any]:
    return {"EID:Slt": f"13:{slot}", "DID": slot, "State": "Onln", "Model": "Synthetic", "SN": serial}


class SummaryTableAndPerDriveEntryTests(unittest.TestCase):
    """A drive named only by a `Drive /cN/eM/sK` entry is kept beside the table (#1305)."""

    def table_and_entry(self) -> dict[str, Any]:
        return {
            "Drive Information": [_row(0, "SYNTH-TABLE-0")],
            "Drive /c0/e13/s1": [_row(1, "SYNTH-ROW-1")],
            "Drive /c0/e13/s1 - Detailed Information": {
                "Drive /c0/e13/s1 Device attributes": {"SN": "SYNTH-DETAIL-1"},
                "Drive /c0/e13/s1 Port Information": {"Connector Name": "C1 x4"},
            },
        }

    def test_the_per_drive_entry_drive_is_kept(self) -> None:
        output = _reply(self.table_and_entry())
        drives = parse_storcli_physical_drives(output, [], "c0")
        self.assertEqual(
            [(drive["slot_key"], drive["serial"]) for drive in drives],
            [("13:0", "SYNTH-TABLE-0"), ("13:1", "SYNTH-DETAIL-1")],
        )
        self.assertEqual(drives[1]["connector_name"], "C1 x4")
        # The table does not list that drive, so the reply is still not a census.
        self.assertFalse(storcli_physical_drive_list_complete(output))

    def test_the_table_row_wins_for_a_drive_both_name(self) -> None:
        data = self.table_and_entry()
        data["Drive /c0/e13/s0"] = [_row(0, "SYNTH-ENTRY-0") | {"State": "Offln"}]
        drives = parse_storcli_physical_drives(_reply(data), [], "c0")
        self.assertEqual([drive["slot_key"] for drive in drives], ["13:0", "13:1"])
        self.assertEqual(drives[0]["state"], "Onln")

    def test_a_table_alone_and_entries_alone_are_unchanged(self) -> None:
        table = {"Drive Information": [_row(0, "SYNTH-TABLE-0")]}
        self.assertEqual(
            [(d["slot_key"], d["serial"]) for d in parse_storcli_physical_drives(_reply(table))],
            [("13:0", "SYNTH-TABLE-0")],
        )
        self.assertTrue(storcli_physical_drive_list_complete(_reply(table)))
        entries = {"Drive /c0/e13/s0": [_row(0, "SYNTH-ROW-0")], "Drive /c0/e13/s1": [_row(1, "SYNTH-ROW-1")]}
        self.assertEqual(
            [(d["slot_key"], d["serial"]) for d in parse_storcli_physical_drives(_reply(entries))],
            [("13:0", "SYNTH-ROW-0"), ("13:1", "SYNTH-ROW-1")],
        )
        self.assertTrue(storcli_physical_drive_list_complete(_reply(entries)))

    def test_the_bay_is_present_through_correlation_and_the_census_still_refuses(self) -> None:
        listing = occupancy.STORCLI_LIST.format(controller=0)
        contradicting = occupancy._storcli_block([occupancy._storcli_drive(0)])
        per_drive = occupancy._storcli_block([occupancy._storcli_drive(1)], form="per-drive")
        contradicting["Response Data"].update(per_drive["Response Data"])
        reply = occupancy._storcli_drives_reply(contradicting)
        # The maintained two-bay fixture: SSH parser, ESXi correlation, storage-view runtime and Fabric.
        result = occupancy.StorcliPerDriveCensusTests.read(self, {listing: reply})
        self.assertEqual(result["census"], {"storcli /c0/eall/sall show all J": False})
        self.assertEqual(result["bays"], [(0, "present", "SANITIZED-C0-S0"), (1, "present", "SANITIZED-C0-S1")])
        self.assertEqual(result["runtime"], [(0, True, True), (1, True, True)])
        self.assertEqual(result["fabric"], [True, True])


if __name__ == "__main__":
    unittest.main()
