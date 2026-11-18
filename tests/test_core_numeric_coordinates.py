"""CORE numeric coordinates: MPR target numbers, enclosure handles and failed-read controller units.

A number the CORE fabric reads from mprutil text must be admitted by a
bounded ASCII grammar:

- an MPR or CAM target number is 1 to 9 ASCII digits; anything else is no
  target, never an exception (#1276).
"""

import unittest

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView
from app.services.sas_fabric import build_sas_fabric_snapshot
from app.services.sas_fabric_core import _parse_mpr_target

SELECTED_ENCLOSURE = "5000000000000001"
EXPANDER_ADDRESS = "5000000000000002"
DISK_ADDRESS = "5000000000000003"
ADAPTER = "Synthetic Adapter:\nBoard Name: Synthetic HBA\nFirmware Revision: 1.2.3\n"


def _slot() -> SlotView:
    return SlotView(slot=0, slot_label="00", row_index=0, column_index=0)


def _core_fabric(outputs: dict[str, str], *, selected: str | None = None, command_failures: list[dict] | None = None):
    return build_sas_fabric_snapshot(
        system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
        snapshot=InventorySnapshot(slots=[_slot()], refresh_interval_seconds=30, selected_enclosure_id=selected),
        ssh_outputs=outputs,
        command_failures=command_failures,
    )


def _expander_header(enclosure_field: str) -> str:
    # mprutil: "  %02d    %08x%08x    %04x       %s     %s       %d" with the
    # enclosure handle printed "%04d".
    return f"  00    {EXPANDER_ADDRESS}    0009       0001     {enclosure_field}       1\n"


def _enclosure_row(handle: int) -> str:
    # mprutil: "  %.2d    %08x%08x    %s       %04x     %s"
    return f"  24    {SELECTED_ENCLOSURE}    000a       {handle:04x}     SES\n"


def _device_row(handle: int, target: str = "0") -> str:
    # bus, target, SAS address, handle, parent, device type, speed, enclosure handle ("%04x"), slot.
    return f"00   {target}   {DISK_ADDRESS} 000b 0009 SAS Target 12.0 {handle:04x} 0\n"


def _handle_outputs(handle: int, expander_field: str) -> dict[str, str]:
    return {
        "mprutil -u 0 show adapter": ADAPTER,
        "mprutil -u 0 show expanders": _expander_header(expander_field),
        "mprutil -u 0 show enclosures": _enclosure_row(handle),
        "mprutil -u 0 show devices": _device_row(handle),
    }


class MprTargetNumberAdmissionTests(unittest.TestCase):
    """#1276: only a bounded run of ASCII digits becomes a target number."""

    def test_ascii_digits_are_a_target(self) -> None:
        # Control: ordinary targets, including the largest admitted length.
        for text, expected in (("0", 0), ("7", 7), ("1023", 1023), ("999999999", 999_999_999), (" 12 ", 12)):
            with self.subTest(text=text):
                self.assertEqual(_parse_mpr_target(text), expected)

    def test_non_ascii_decimal_digit_is_no_target(self) -> None:
        for text in ("\u0663", "\u0661\u0662", "\uff13", "1\u0663"):
            with self.subTest(text=ascii(text)):
                self.assertIsNone(_parse_mpr_target(text))

    def test_digit_character_that_is_not_decimal_is_no_target_without_raising(self) -> None:
        for text in ("\u00b2", "1\u00b2", "\u2460"):
            with self.subTest(text=ascii(text)):
                self.assertIsNone(_parse_mpr_target(text))

    def test_overlong_digit_run_is_no_target_without_raising(self) -> None:
        for text in ("1" * 10, "9" * 5000):
            with self.subTest(length=len(text)):
                self.assertIsNone(_parse_mpr_target(text))

    def test_overlong_device_row_target_does_not_stop_the_fabric(self) -> None:
        outputs = {**_handle_outputs(9, "0009"), "mprutil -u 0 show devices": _device_row(9, target="9" * 5000)}

        fabric = _core_fabric(outputs, selected=SELECTED_ENCLOSURE)

        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        self.assertEqual(len([node for node in fabric.nodes if node.kind == "expander"]), 1)


if __name__ == "__main__":
    unittest.main()
