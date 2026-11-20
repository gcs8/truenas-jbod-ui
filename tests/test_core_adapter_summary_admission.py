"""CORE adapter-summary admission: exact device names and a bounded unit list (#1316).

`mprutil show adapters` prints one row per controller, its device field the
exact name `/dev/mpr<unit>`. A row is admitted only when that whole field is
the adapter device name with a bounded unit number. A device field with any
text before or after the name is a rejected row: it plans no reads and is
counted without its text being copied anywhere.

The unique-unit list has a runaway bound far above real hardware. Units past
the bound are counted and not read; every unit inside it is still read.
Either count makes Storage Fabric report a fixed incomplete-read warning.
"""

import json
import unittest

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot
from app.services import sas_fabric_parsers
from app.services.sas_fabric import build_sas_fabric_snapshot
from app.services.sas_fabric_parsers import (
    CORE_MPRUTIL_UNIT_SUBCOMMANDS,
    build_core_mprutil_unit_commands,
    discover_mpr_units_from_adapter_summary,
)

BOARD = " SAS3008 Synthetic HBA 1.2.3\n"
HEADING = "Adapter     Chip           Board Name        Firmware\n"
ADAPTER = "Synthetic Adapter:\nBoard Name: Synthetic HBA\nFirmware Revision: 1.2.3\n"
# Text placed around a device name; none of it may reach the fabric's output.
DECORATION = "synthdecor"


def _summary(*devices: str) -> str:
    return HEADING + "".join(f"{device}{BOARD}" for device in devices)


def _unit_commands(unit: int) -> list[str]:
    return [f"sudo -n /usr/sbin/mprutil -u {unit} show {subcommand}" for subcommand in CORE_MPRUTIL_UNIT_SUBCOMMANDS]


def _core_fabric(outputs: dict[str, str]):
    return build_sas_fabric_snapshot(
        system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
        snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30),
        ssh_outputs=outputs,
    )


def _unit_limit() -> int:
    limit = getattr(sas_fabric_parsers, "MAX_CORE_MPR_SUMMARY_UNITS", None)
    if limit is None:
        raise AssertionError("the adapter summary has no unique-unit bound")
    return limit


PREFIXED_DEVICES = {
    "text before the name": f"/dev/{DECORATION}.mpr0",
    "dashed text before the name": f"/dev/{DECORATION}-mpr0",
    "path before the name": f"/dev/{DECORATION}/dev/mpr0",
}
SUFFIXED_DEVICES = {
    "text after the unit": f"/dev/mpr0.{DECORATION}",
    "dashed text after the unit": f"/dev/mpr0-{DECORATION}",
    "colon after the unit": "/dev/mpr0:",
}


class AdapterDeviceFieldAdmissionTests(unittest.TestCase):
    def test_exact_device_name_plans_every_unit_read(self) -> None:
        # Control: the device field as mprutil prints it.
        summary = _summary("/dev/mpr0", "/dev/mpr10")

        self.assertEqual(discover_mpr_units_from_adapter_summary(summary), [0, 10])
        self.assertEqual(build_core_mprutil_unit_commands(summary), _unit_commands(0) + _unit_commands(10))

    def test_text_before_the_device_name_plans_no_reads(self) -> None:
        for name, device in PREFIXED_DEVICES.items():
            with self.subTest(name):
                summary = _summary(device)
                self.assertEqual(discover_mpr_units_from_adapter_summary(summary), [])
                self.assertEqual(build_core_mprutil_unit_commands(summary), [])

    def test_text_after_the_unit_plans_no_reads(self) -> None:
        for name, device in SUFFIXED_DEVICES.items():
            with self.subTest(name):
                summary = _summary(device)
                self.assertEqual(discover_mpr_units_from_adapter_summary(summary), [])
                self.assertEqual(build_core_mprutil_unit_commands(summary), [])

    def test_decorated_row_does_not_stop_the_exact_rows(self) -> None:
        summary = _summary("/dev/mpr1", *PREFIXED_DEVICES.values(), *SUFFIXED_DEVICES.values(), "/dev/mpr2")

        self.assertEqual(build_core_mprutil_unit_commands(summary), _unit_commands(1) + _unit_commands(2))


class AdapterSummaryUnitBoundTests(unittest.TestCase):
    def test_bound_sits_far_above_real_hardware(self) -> None:
        # A host carries a handful of HBAs; the bound is a runaway guard.
        self.assertGreaterEqual(_unit_limit(), 64)

    def test_real_hardware_unit_count_is_read_whole(self) -> None:
        # Control: 64 controllers are all planned, five reads each.
        summary = _summary(*(f"/dev/mpr{unit}" for unit in range(64)))

        self.assertEqual(discover_mpr_units_from_adapter_summary(summary), list(range(64)))
        self.assertEqual(len(build_core_mprutil_unit_commands(summary)), 64 * len(CORE_MPRUTIL_UNIT_SUBCOMMANDS))

    def test_runaway_unit_list_plans_only_the_bounded_units(self) -> None:
        limit = _unit_limit()
        summary = _summary(*(f"/dev/mpr{unit}" for unit in range(300)))

        units = discover_mpr_units_from_adapter_summary(summary)
        commands = build_core_mprutil_unit_commands(summary)

        self.assertLess(limit, 300)
        self.assertEqual(units, list(range(limit)))
        self.assertEqual(commands, [command for unit in range(limit) for command in _unit_commands(unit)])

    def test_repeated_rows_are_one_unit(self) -> None:
        summary = _summary(*(["/dev/mpr3"] * 300))

        self.assertEqual(discover_mpr_units_from_adapter_summary(summary), [3])
        self.assertEqual(build_core_mprutil_unit_commands(summary), _unit_commands(3))


class AdapterSummaryFabricWarningTests(unittest.TestCase):
    def _assert_not_echoed(self, fabric) -> None:
        serialized = json.dumps(fabric.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn(DECORATION, serialized)
        self.assertTrue(all(len(warning) < 300 for warning in fabric.warnings), fabric.warnings)

    def test_exact_summary_reports_no_rejection(self) -> None:
        # Control: a read unit with an exact device name raises no summary warning.
        fabric = _core_fabric({"mprutil show adapters": _summary("/dev/mpr0"), "mprutil -u 0 show adapter": ADAPTER})

        self.assertEqual(fabric.raw["controller_read"].get("rejected_summary_rows"), 0)
        self.assertEqual(fabric.raw["controller_read"].get("summary_units_over_limit"), 0)
        self.assertFalse(any("adapter list" in warning for warning in fabric.warnings), fabric.warnings)

    def test_decorated_rows_are_counted_warned_and_not_echoed(self) -> None:
        devices = [*PREFIXED_DEVICES.values()]
        summary = _summary("/dev/mpr0", *devices)
        fabric = _core_fabric({"mprutil show adapters": summary, "mprutil -u 0 show adapter": ADAPTER})

        read = fabric.raw["controller_read"]
        self.assertEqual(read.get("rejected_summary_rows"), len(devices))
        self.assertEqual(read["units"], [{"controller": "mpr0", "result": "read"}])
        self.assertEqual(read["counts"]["controllers"], "partial")
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        self.assertIn(
            f"The CORE mprutil adapter list had {len(devices)} rows that are not an adapter device name, "
            "so they were not read and controller counts may be incomplete.",
            fabric.warnings,
        )
        self._assert_not_echoed(fabric)

    def test_suffixed_rows_are_not_read_and_not_echoed(self) -> None:
        devices = [*SUFFIXED_DEVICES.values()]
        fabric = _core_fabric({"mprutil show adapters": _summary("/dev/mpr0", *devices), "mprutil -u 0 show adapter": ADAPTER})

        read = fabric.raw["controller_read"]
        self.assertEqual(read["units"], [{"controller": "mpr0", "result": "read"}])
        self.assertEqual(read["counts"]["controllers"], "partial")
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        self._assert_not_echoed(fabric)

    def test_units_past_the_bound_are_counted_and_warned(self) -> None:
        limit = _unit_limit()
        outputs = {"mprutil show adapters": _summary(*(f"/dev/mpr{unit}" for unit in range(300)))}
        outputs.update({f"mprutil -u {unit} show adapter": ADAPTER for unit in range(limit)})
        fabric = _core_fabric(outputs)

        read = fabric.raw["controller_read"]
        self.assertEqual(read.get("summary_units_over_limit"), 300 - limit)
        self.assertEqual(len(read["units"]), limit)
        self.assertTrue(all(item["result"] == "read" for item in read["units"]))
        self.assertEqual(len(fabric.controllers), limit)
        self.assertEqual(read["counts"]["controllers"], "partial")
        self.assertIn(
            f"The CORE mprutil adapter list named more than {limit} controller units; "
            f"{300 - limit} were not read, so controller, expander and enclosure counts may be incomplete.",
            fabric.warnings,
        )


if __name__ == "__main__":
    unittest.main()
