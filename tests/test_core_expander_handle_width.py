"""CORE expander headers with a five-digit enclosure handle (#1416).

mprutil prints an expander's enclosure handle in decimal ("%04d"). A 16-bit
handle can need five digits (10000 to 65535), so both header forms must admit
five decimal digits. A header-shaped line that is still not admitted means the
expander list was not read whole: the list is unreadable and its count
partial, never silently complete.
"""

import unittest

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView
from app.services.sas_fabric import build_sas_fabric_snapshot
from app.services.sas_fabric_parsers import parse_mpr_expanders

# Synthetic SAS addresses, built so the text holds no address-shaped literal.
ENCLOSURE_A = f"5{0x11:015x}"
ENCLOSURE_B = f"5{0x12:015x}"
EXPANDER_A = f"5{0x21:015x}"
EXPANDER_B = f"5{0x22:015x}"
ADAPTER = "Synthetic Adapter:\nBoard Name: Synthetic HBA\nFirmware Revision: 1.2.3\n"
EXPANDER_HEADING = "NumPhys   SAS Address     DevHandle   Parent  EncHandle  SAS Level\n"
ENCLOSURE_HEADING = "Slots      Logical ID     SEPHandle  EncHandle    Type\n"
LIST_WARNING = (
    "CORE controller mpr0 did not return a readable expander or enclosure list, so those counts may be incomplete."
)


def _header(address: str, dev_handle: str, enclosure_field: str) -> str:
    # mprutil: "  %02d    %08x%08x    %04x       %s     %04d       %d".
    return f"  00    {address}    {dev_handle}       0001     {enclosure_field}       1\n"


def _enclosure_row(address: str, handle: int) -> str:
    # mprutil: "  %.2d    %08x%08x    %s       %04x     %s".
    return f"  24    {address}    000a       {handle:04x}     SES\n"


def _outputs(expanders: str) -> dict[str, str]:
    return {
        "mprutil -u 0 show adapter": ADAPTER,
        "mprutil -u 0 show expanders": EXPANDER_HEADING + expanders,
        "mprutil -u 0 show enclosures": ENCLOSURE_HEADING
        + _enclosure_row(ENCLOSURE_A, 9)
        + _enclosure_row(ENCLOSURE_B, 10000),
        "mprutil -u 0 show devices": "B____T    SAS Address      Handle  Parent    Device        Speed Enc  Slot  Wdt\n",
    }


def _core_fabric(outputs: dict[str, str], selected: str):
    return build_sas_fabric_snapshot(
        system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
        snapshot=InventorySnapshot(
            slots=[SlotView(slot=0, slot_label="00", row_index=0, column_index=0)],
            refresh_interval_seconds=30,
            selected_enclosure_id=selected,
        ),
        ssh_outputs=outputs,
    )


def _one_expander(text: str) -> dict:
    parsed = parse_mpr_expanders(text)
    if len(parsed) != 1:
        raise AssertionError(f"expected one expander header read, got {len(parsed)}")
    return parsed[0]


ORDINARY = _header(EXPANDER_A, "0009", "0009")
FIVE_DIGIT = _header(EXPANDER_B, "000c", "10000")


class FiveDigitHandleParseTests(unittest.TestCase):
    def test_four_digit_handle_is_read(self) -> None:
        # Control.
        parsed = _one_expander(ORDINARY)
        self.assertEqual(parsed["enc_handle"], "0009")

    def test_five_digit_handle_in_the_printed_columns_is_read(self) -> None:
        for printed, expected in (("10000", "2710"), ("65535", "ffff")):
            with self.subTest(printed=printed):
                parsed = _one_expander(_header(EXPANDER_B, "000c", printed))
                self.assertEqual(parsed["sas_address"], EXPANDER_B)
                self.assertEqual(parsed["enc_handle"], expected)

    def test_five_digit_handle_with_other_spacing_is_read(self) -> None:
        parsed = _one_expander(f"0 {EXPANDER_B} 000c 0001 10000 1\n")
        self.assertEqual(parsed["enc_handle"], "2710")

    def test_five_digits_past_sixteen_bits_name_no_enclosure(self) -> None:
        # Not a 16-bit handle: the expander is kept, its enclosure is unknown.
        parsed = _one_expander(_header(EXPANDER_B, "000c", "70000"))
        self.assertEqual(parsed["sas_address"], EXPANDER_B)
        self.assertIsNone(parsed["enc_handle"])

    def test_mixed_reply_reads_both_expanders(self) -> None:
        parsed = parse_mpr_expanders(ORDINARY + FIVE_DIGIT)
        self.assertEqual([row["sas_address"] for row in parsed], [EXPANDER_A, EXPANDER_B])
        self.assertEqual([row["enc_handle"] for row in parsed], ["0009", "2710"])


class ExpanderListCompletenessTests(unittest.TestCase):
    def _read(self, expanders: str, selected: str):
        # The fabric draws the selected enclosure's scope.
        fabric = _core_fabric(_outputs(expanders), selected)
        read = fabric.raw["controller_read"]
        nodes = [node for node in fabric.nodes if node.kind == "expander"]
        links = [link for link in fabric.links if link.kind == "expander-enclosure"]
        return fabric, read["lists"]["mpr0"]["expanders"], read["counts"]["expanders"], nodes, links

    def test_ordinary_reply_is_read_whole(self) -> None:
        # Control.
        fabric, state, count, nodes, links = self._read(ORDINARY, ENCLOSURE_A)
        self.assertEqual((state, count), ("read", "complete"))
        self.assertEqual([node.raw_id for node in nodes], [EXPANDER_A])
        self.assertEqual(len(links), 1)
        self.assertNotIn(LIST_WARNING, fabric.warnings)

    def test_mixed_reply_with_a_five_digit_handle_reads_every_expander(self) -> None:
        fabric, state, count, nodes, links = self._read(ORDINARY + FIVE_DIGIT, ENCLOSURE_B)

        self.assertEqual((state, count), ("read", "complete"))
        # The expander behind the five-digit handle is drawn in its enclosure's scope.
        self.assertEqual([node.raw_id for node in nodes], [EXPANDER_B])
        [enclosure] = [node for node in fabric.nodes if node.kind == "mpr-enclosure"]
        self.assertEqual(enclosure.raw_id, ENCLOSURE_B)
        self.assertEqual([(link.source, link.target) for link in links], [(nodes[0].id, enclosure.id)])
        self.assertNotIn(LIST_WARNING, fabric.warnings)

    def test_header_line_not_admitted_makes_the_list_incomplete(self) -> None:
        # Six digits do not fit a 16-bit handle; a hexadecimal letter in a
        # five-character field is not decimal. Either way one expander's
        # header was not read, so the list is not whole.
        for name, field in {"six digits": "100000", "letter in five characters": "1000a"}.items():
            with self.subTest(name):
                fabric, state, count, nodes, _links = self._read(
                    ORDINARY + _header(EXPANDER_B, "000c", field), ENCLOSURE_A
                )

                self.assertEqual([node.raw_id for node in nodes], [EXPANDER_A])
                self.assertEqual((state, count), ("unreadable", "partial"))
                self.assertIn(LIST_WARNING, fabric.warnings)


if __name__ == "__main__":
    unittest.main()
