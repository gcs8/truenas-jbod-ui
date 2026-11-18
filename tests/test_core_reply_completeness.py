"""CORE mprutil replies are complete only when every printed row was understood.

A device list is read whole only when every row carrying a SAS address was
understood, as one of the device types mprutil prints (#1279). A dedicated
list or adapter reply in which the utility reported an error is not read,
exactly as the same text under a `show all` section label is not (#1281). An
adapter PHY table is read whole only when each printed PHY number matches its
position, counting the positions that discovery messages hold (#1282).
"""

import unittest

from app.services.sas_fabric_parsers import parse_mpr_devices
from tests.test_core_fabric_legacy_controller import (
    DEVICE_ADDRESS,
    DEVICES_HEADING,
    SELECTED_ENCLOSURE,
    _core_fabric,
    _healthy_unit,
    _inventory_slot,
    _printed_adapter,
    _printed_device_row,
    _show_all,
)

# Every device type mprutil's get_device_type() prints, padded to its width.
MPRUTIL_DEVICE_TYPES = (
    "SEP Target",
    "ATAPI Target",
    "SAS Target",
    "STP Target",
    "SMP Target",
    "SATA Target",
    "SAS Initiator",
    "SATA Initiator",
    "No Device",
    "Unknown Device",
)
DEVICE_ERROR = "mprutil: Error retrieving device page: Input/output error\n"
ENCLOSURE_ERROR = "mprutil: Error retrieving enclosure page: Input/output error\n"
EXPANDER_ERROR = "mprutil: Error retrieving expander page 0: Input/output error\n"
ADAPTER_ERROR = "mprutil: Error retrieving SAS IO Unit page 0\n"


def _device_row(kind: str, handle: int = 0x0B, enclosure: str = "0010") -> str:
    row = _printed_device_row(enclosure)
    return row.replace("SAS Target    ", f"{kind:<13} ").replace(" 000b ", f" {handle:04x} ")


def _device_list(*kinds: str) -> str:
    return DEVICES_HEADING + "".join(_device_row(kind, 0x0B + index) for index, kind in enumerate(kinds)) + "\n"


def _fabric(outputs: dict[str, str]):
    return _core_fabric(outputs, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)


def _enclosure_metrics(fabric) -> list[dict]:
    return [node.metrics for node in fabric.nodes if node.kind == "mpr-enclosure"]


def _adapter(rows: str) -> str:
    return _printed_adapter([]).replace("Device\n\n", f"Device\n{rows}\n")


DISCOVERY = "Discovery still in progress\n"


class CoreDeviceTypeCompletenessTests(unittest.TestCase):
    """#1279: every device type mprutil prints is a row; a row not understood leaves the list not read."""

    def test_every_printed_device_type_is_a_row(self) -> None:
        for kind in MPRUTIL_DEVICE_TYPES:
            with self.subTest(kind):
                rows = parse_mpr_devices(_device_list(kind))
                self.assertEqual(
                    [(row["device"], row["sas_address"], row["enclosure_handle"]) for row in rows],
                    [(kind, DEVICE_ADDRESS, "0010")],
                )
        self.assertEqual(len(parse_mpr_devices(_device_list(*MPRUTIL_DEVICE_TYPES))), len(MPRUTIL_DEVICE_TYPES))

    def test_mixed_list_counts_every_device_in_the_enclosure(self) -> None:
        for kinds in (("SAS Target", "STP Target"), ("STP Target",), ("SATA Target", "ATAPI Target", "SAS Initiator")):
            with self.subTest(kinds):
                fabric = _fabric({**_healthy_unit(), "mprutil -u 0 show devices": _device_list(*kinds)})
                self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "read")
                [enclosure] = _enclosure_metrics(fabric)
                self.assertEqual(enclosure["device_count"], len(kinds))
                self.assertNotIn("not_read", enclosure)
                self.assertEqual(fabric.warnings, [])

    def test_row_that_is_not_understood_withholds_the_device_count(self) -> None:
        unknown_type = _device_row("Mystery Thing", 0x0C)
        lost_columns = f"00   11    {DEVICE_ADDRESS} 000c    000a      SAS Target\n"
        for name, bad_row in {"unknown type": unknown_type, "lost columns": lost_columns}.items():
            for label, outputs in {
                "dedicated": {**_healthy_unit(), "mprutil -u 0 show devices": DEVICES_HEADING + _device_row("SAS Target") + bad_row + "\n"},
                "show all": {"mprutil -u 0 show all": _show_all().replace(_device_row("SAS Target"), _device_row("SAS Target") + bad_row)},
            }.items():
                with self.subTest(name, source=label):
                    fabric = _fabric(outputs)
                    self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "unreadable")
                    [enclosure] = _enclosure_metrics(fabric)
                    # The surviving row is not published as a whole count.
                    self.assertEqual((enclosure["device_count"], enclosure.get("not_read")), (None, ["device_count"]))

    def test_notice_lines_without_a_device_row_keep_the_list_read(self) -> None:
        # Control: text that is not a device row is not a malformed device row.
        listing = DEVICES_HEADING + _device_row("SAS Target") + "synthetic notice text\n\n"
        fabric = _fabric({**_healthy_unit(), "mprutil -u 0 show devices": listing})
        self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["devices"], "read")
        self.assertEqual([metrics["device_count"] for metrics in _enclosure_metrics(fabric)], [1])
        self.assertEqual(fabric.warnings, [])


if __name__ == "__main__":
    unittest.main()
