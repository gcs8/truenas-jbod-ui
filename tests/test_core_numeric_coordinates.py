"""CORE numeric coordinates: MPR target numbers, enclosure handles and failed-read controller units.

Three numbers the CORE fabric reads from mprutil text must be admitted by one
bounded ASCII grammar and compared as numbers:

- an MPR or CAM target number is 1 to 9 ASCII digits; anything else is no
  target, never an exception (#1276);
- the expander list prints its enclosure handle in decimal ("%04d") while the
  device and enclosure lists print the same handle in hexadecimal ("%04x"), so
  the expander's field is read as decimal before it is compared (#1280);
- a failed numbered mprutil read names its controller only when the unit is a
  unit number the fabric admits; a rejected unit is fixed metadata and a count,
  not a copy of the token (#1317).
"""

import json
import unittest

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView
from app.services.inventory import InventoryService
from app.services.sas_fabric import build_sas_fabric_snapshot
from app.services.sas_fabric_core import _parse_mpr_target
from app.services.sas_fabric_parsers import parse_mpr_expanders
from app.services.ssh_probe import SSHCommandResult

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


class MprEnclosureHandleBaseTests(unittest.TestCase):
    """#1280: the expander's decimal handle meets the hexadecimal lists as one number."""

    def _selected_links(self, handle: int, expander_field: str):
        fabric = _core_fabric(_handle_outputs(handle, expander_field), selected=SELECTED_ENCLOSURE)
        expanders = [node for node in fabric.nodes if node.kind == "expander"]
        selected = [node for node in expanders if node.raw.get("fabric_scope") == "selected_enclosure"]
        links = [link for link in fabric.links if link.kind == "expander-enclosure"]
        return fabric, expanders, selected, links

    def test_handle_below_ten_associates(self) -> None:
        # Control: 9 prints "0009" in both bases.
        _fabric, expanders, selected, links = self._selected_links(9, "0009")

        self.assertEqual(len(expanders), 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(links), 1)

    def test_handles_from_ten_associate_across_the_two_print_bases(self) -> None:
        for handle in (10, 16, 255, 9999):
            with self.subTest(handle=handle):
                fabric, expanders, selected, links = self._selected_links(handle, f"{handle:04d}")

                self.assertEqual(len(expanders), 1)
                self.assertEqual(len(selected), 1, [node.raw for node in expanders])
                self.assertEqual(len(links), 1)
                [enclosure] = [node for node in fabric.nodes if node.kind == "mpr-enclosure"]
                self.assertEqual(links[0].source, expanders[0].id)
                self.assertEqual(links[0].target, enclosure.id)

    def test_same_digits_in_both_lists_are_different_handles(self) -> None:
        # Decimal 16 and hexadecimal 0x16 (22) are different enclosures.
        _fabric, _expanders, selected, links = self._selected_links(0x16, "0016")

        self.assertEqual(selected, [])
        self.assertEqual(links, [])

    def test_expander_handle_is_read_as_decimal(self) -> None:
        [parsed] = parse_mpr_expanders(_expander_header("0016"))
        self.assertEqual(parsed["enc_handle"], "0010")

        [parsed] = parse_mpr_expanders(_expander_header("0009"))
        self.assertEqual(parsed["enc_handle"], "0009")

    def test_expander_handle_that_is_not_decimal_names_no_enclosure(self) -> None:
        # Not guessed as hexadecimal: the field is printed in decimal.
        [parsed] = parse_mpr_expanders(_expander_header("000a"))
        self.assertIsNone(parsed["enc_handle"])

        _fabric, _expanders, selected, links = self._selected_links(10, "000a")
        self.assertEqual(selected, [])
        self.assertEqual(links, [])


def _failure_records(*commands: str) -> list[dict]:
    results = [SSHCommandResult(command=command, ok=False, exit_code=1) for command in commands]
    return InventoryService._ssh_failure_details(results)


class FailedReadControllerUnitTests(unittest.TestCase):
    """#1317: a failed read's controller uses the fabric's unit grammar."""

    REJECTED_UNITS = {
        "2000 ASCII digits": "9" * 2000,
        "five ASCII digits": "12345",
        "non-ASCII decimal digit": "\u0663",
    }

    def test_admitted_unit_keeps_its_controller(self) -> None:
        # Control: an ordinary bounded unit stays associated with its controller.
        for unit in ("0", "7", "9999"):
            with self.subTest(unit=unit):
                [record] = _failure_records(f"sudo -n /usr/sbin/mprutil -u {unit} show adapter")
                self.assertEqual(record["controller"], f"mpr{unit}")
                self.assertNotIn("controller_rejected", record)

        fabric = _core_fabric({}, command_failures=_failure_records("sudo -n /usr/sbin/mprutil -u 7 show adapter"))
        self.assertEqual(fabric.raw["controller_read"]["units"], [{"controller": "mpr7", "result": "command_failed"}])
        self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 0)

    def test_rejected_unit_is_fixed_metadata_not_a_copy_of_the_token(self) -> None:
        for name, unit in self.REJECTED_UNITS.items():
            with self.subTest(name):
                [record] = _failure_records(f"sudo -n /usr/sbin/mprutil -u {unit} show adapter")

                self.assertFalse("controller" in record, sorted(record))
                self.assertEqual(record.get("controller_rejected"), "not_a_unit_number")
                self.assertEqual(record["context"], "sas_fabric_mprutil_adapter")
                self.assertFalse(unit in json.dumps(record, ensure_ascii=False), "the unit token was published")

    def test_rejected_unit_is_counted_and_not_published_by_the_fabric(self) -> None:
        for name, unit in self.REJECTED_UNITS.items():
            with self.subTest(name):
                failures = _failure_records(
                    f"sudo -n /usr/sbin/mprutil -u {unit} show adapter",
                    f"sudo -n /usr/sbin/mprutil -u {unit} show devices",
                )
                fabric = _core_fabric({"mprutil -u 0 show adapter": ADAPTER}, command_failures=failures)

                self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 1)
                self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "partial")
                serialized = json.dumps(fabric.model_dump(mode="json"), ensure_ascii=False)
                self.assertFalse(f"mpr{unit}" in serialized, "the unit token was published")
                for record in fabric.raw["command_failures"]:
                    self.assertFalse("controller" in record, sorted(record))
                    self.assertTrue(all(len(str(value)) < 100 for value in record.values()), sorted(record))


if __name__ == "__main__":
    unittest.main()
