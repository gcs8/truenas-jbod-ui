"""Malformed MPR and SES text is rejected with bounded parser work (#1216).

Python's regex engine exposes no step counter, so these tests count work
instead of timing it. Each regex call made by the parser module is charged the
square of the text region it was handed, which is the worst case for the
patterns these parsers use, and each Python line executed in the parser module
is charged one unit. Doubling a malformed input may at most roughly double that
count, and no regex may be handed a region that grows with the input.
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from unittest import mock

from app.services import parsers
from app.services.parsers import (
    parse_sesutil_map,
    parse_sesutil_show_enclosures,
    parse_sg_ses_aes,
    parse_sg_ses_enclosure_status,
    parse_sg_ses_join_filter,
)
from app.services.sas_diagnostics import scsi
from app.services.sas_diagnostics.scsi import decode_scsi_cdb_message
from app.services.sas_fabric_parsers import parse_mpr_dmesg_events

SIZES = (2000, 4000, 8000)
# Doubling the input of a linear pass doubles the count plus a constant.
MAX_GROWTH_PER_DOUBLING = 2.5


class _WorkMeter:
    def __init__(self) -> None:
        self.regex_calls = 0
        self.regex_work = 0
        self.max_region = 0
        self.python_lines = 0

    @property
    def work(self) -> int:
        return self.regex_work + self.python_lines

    def charge(self, string: str, pos: int = 0, endpos: int | None = None) -> None:
        length = len(string)
        start = min(max(pos, 0), length)
        end = length if endpos is None else min(max(endpos, 0), length)
        region = max(end - start, 0)
        self.regex_calls += 1
        self.regex_work += region * region
        self.max_region = max(self.max_region, region)


class _MeteredPattern:
    def __init__(self, pattern: re.Pattern[str], meter: _WorkMeter) -> None:
        self._pattern = pattern
        self._meter = meter

    def __getattr__(self, name: str):  # noqa: ANN204 - attribute passthrough
        return getattr(self._pattern, name)

    def search(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos)
        return self._pattern.search(string, pos, endpos)

    def match(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos)
        return self._pattern.match(string, pos, endpos)

    def fullmatch(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos)
        return self._pattern.fullmatch(string, pos, endpos)

    def finditer(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos)
        return self._pattern.finditer(string, pos, endpos)

    def findall(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos)
        return self._pattern.findall(string, pos, endpos)

    def sub(self, repl, string, count=0):  # noqa: ANN001, ANN201
        self._meter.charge(string)
        return self._pattern.sub(repl, string, count=count)

    def subn(self, repl, string, count=0):  # noqa: ANN001, ANN201
        self._meter.charge(string)
        return self._pattern.subn(repl, string, count=count)

    def split(self, string, maxsplit=0):  # noqa: ANN001, ANN201
        self._meter.charge(string)
        return self._pattern.split(string, maxsplit=maxsplit)


def _metered_re(meter: _WorkMeter) -> types.ModuleType:
    proxy = types.ModuleType("re")
    proxy.__dict__.update({name: value for name, value in vars(re).items() if not name.startswith("__")})

    def compile_(pattern, flags=0):  # noqa: ANN001, ANN202
        compiled = pattern._pattern if isinstance(pattern, _MeteredPattern) else re.compile(pattern, flags)
        return _MeteredPattern(compiled, meter)

    setattr(proxy, "compile", compile_)
    for name in ("search", "match", "fullmatch", "finditer", "findall"):
        def module_call(pattern, string, flags=0, _name=name):  # noqa: ANN001, ANN202
            return getattr(compile_(pattern, flags), _name)(string)

        setattr(proxy, name, module_call)
    for name in ("sub", "subn"):
        def module_sub(pattern, repl, string, count=0, flags=0, _name=name):  # noqa: ANN001, ANN202
            return getattr(compile_(pattern, flags), _name)(repl, string, count=count)

        setattr(proxy, name, module_sub)

    def module_split(pattern, string, maxsplit=0, flags=0):  # noqa: ANN001, ANN202
        return compile_(pattern, flags).split(string, maxsplit=maxsplit)

    setattr(proxy, "split", module_split)
    return proxy


def measure(module: types.ModuleType, call) -> tuple[object, _WorkMeter]:  # noqa: ANN001
    """Run `call` and count regex and Python-line work inside `module`."""

    meter = _WorkMeter()
    target_file = module.__file__

    def local_trace(frame, event, arg):  # noqa: ANN001, ANN202 - trace protocol
        if event == "line":
            meter.python_lines += 1
        return local_trace

    def global_trace(frame, event, arg):  # noqa: ANN001, ANN202 - trace protocol
        if frame.f_code.co_filename == target_file:
            return local_trace
        return None

    patches: list = [mock.patch.object(module, "re", _metered_re(meter))]
    for name, value in vars(module).items():
        if isinstance(value, re.Pattern):
            patches.append(mock.patch.object(module, name, _MeteredPattern(value, meter)))
    for patcher in patches:
        patcher.start()
    previous = sys.gettrace()
    sys.settrace(global_trace)
    try:
        result = call()
    finally:
        sys.settrace(previous)
        for patcher in reversed(patches):
            patcher.stop()
    return result, meter


class BoundedWorkAssertions(unittest.TestCase):
    def assert_bounded_rejection(self, module, build, parse, rejected) -> None:  # noqa: ANN001
        previous_work: int | None = None
        regions: list[tuple[int, int]] = []
        for size in SIZES:
            text = build(size)
            result, meter = measure(module, lambda: parse(text))
            self.assertEqual(result, rejected, f"{size}-character input was not rejected")
            if previous_work is not None:
                self.assertLessEqual(
                    meter.work,
                    previous_work * MAX_GROWTH_PER_DOUBLING,
                    f"work grew from {previous_work} to {meter.work} when the input doubled to {len(text)}",
                )
            previous_work = meter.work
            regions.append((meter.max_region, len(text)))
        for max_region, length in regions:
            self.assertLess(
                max_region,
                SIZES[0],
                f"a regex was handed {max_region} characters of a {length}-character input",
            )


class MprCdbDecodeWorkTests(BoundedWorkAssertions):
    def test_operation_run_without_separator_is_rejected_with_bounded_work(self) -> None:
        self.assert_bounded_rejection(
            scsi,
            lambda size: "(da0:mpr0:0:1:0): " + "A" * size + " CDB: 00",
            decode_scsi_cdb_message,
            {},
        )

    def test_operation_run_with_unparsable_cdb_is_rejected_with_bounded_work(self) -> None:
        self.assert_bounded_rejection(
            scsi,
            lambda size: "(da0:mpr0:0:1:0): " + "A" * size + "(10. CDB: zz",
            decode_scsi_cdb_message,
            {},
        )

    def test_dmesg_event_pipeline_decodes_malformed_cdb_with_bounded_work(self) -> None:
        def parse(text: str):  # noqa: ANN202
            parsed = parse_mpr_dmesg_events(text)
            return parsed["event_count"], dict(parsed["by_controller"]["mpr0"]["operation_counts"])

        self.assert_bounded_rejection(
            scsi,
            lambda size: "(da0:mpr0:0:1:0): " + "A" * size + " CDB: 00",
            parse,
            (1, {}),
        )

    def test_overlong_operation_name_is_not_decoded(self) -> None:
        cdb = "28 00 00 00 00 10 00 00 01 00"
        control = decode_scsi_cdb_message(f"(da0:mpr0:0:1:0): READ(10). CDB: {cdb}")
        self.assertEqual(control["operation"], "READ(10)")
        self.assertEqual(control["reported_operation"], "READ(10)")
        self.assertEqual(control["cdb_hex"], cdb)

        decoded = decode_scsi_cdb_message(f"(da0:mpr0:0:1:0): {'A' * 200} READ(10). CDB: {cdb}")

        self.assertEqual(decoded, {})

    def test_overlong_cdb_text_is_not_decoded(self) -> None:
        # A variable-length CDB is at most 260 bytes.
        control = decode_scsi_cdb_message("VARIABLE LENGTH CDB. CDB: " + " ".join(["7f"] + ["00"] * 259))
        self.assertEqual(control["byte_count"], 260)
        self.assertEqual(control["opcode"], "0x7f")

        decoded = decode_scsi_cdb_message("VARIABLE LENGTH CDB. CDB: " + " ".join(["7f"] + ["00"] * 400))

        self.assertEqual(decoded, {})

    def test_separator_search_keeps_the_first_well_formed_operation(self) -> None:
        cases = {
            "(da0:mpr0:0:1:0): SYNCHRONIZE CACHE(10). CDB: 35 00 00 00 00 00 00 00 00 00": (
                "SYNCHRONIZE CACHE(10)",
                "35 00 00 00 00 00 00 00 00 00",
            ),
            "noise. CDB: zz then WRITE(10). CDB: 2a 00 00 00 00 08 00 00 08 00": (
                "WRITE(10)",
                "2a 00 00 00 00 08 00 00 08 00",
            ),
            "x 12 READ(16).\tCDB:  88 00 00 00 00 00 00 00 00 10 00 00 00 08 00 00": (
                "READ(16)",
                "88 00 00 00 00 00 00 00 00 10 00 00 00 08 00 00",
            ),
            "TEST UNIT READY. CDB: 00 00 00 00 00 00 and READ(10). CDB: 28": (
                "TEST UNIT READY",
                "00 00 00 00 00 00",
            ),
        }
        for message, (reported_operation, cdb_hex) in cases.items():
            with self.subTest(message=message):
                decoded = decode_scsi_cdb_message(message)
                self.assertEqual(decoded["reported_operation"], reported_operation)
                self.assertEqual(decoded["cdb_hex"], cdb_hex)

        for message in ("READ(10) CDB: 28 00", "read(10). CDB: 28 00", "READ(10). CDB:28 00", "(10). CDB: 28"):
            with self.subTest(message=message):
                self.assertEqual(decode_scsi_cdb_message(message), {})


SES_PARSERS = {
    "sesutil map": (parse_sesutil_map, []),
    "sesutil show": (parse_sesutil_show_enclosures, []),
    "sg_ses aes": (parse_sg_ses_aes, None),
    "sg_ses ec": (parse_sg_ses_enclosure_status, None),
    "sg_ses join": (parse_sg_ses_join_filter, None),
}


class SesPreCountWorkTests(BoundedWorkAssertions):
    def test_whitespace_shapes_are_rejected_with_bounded_work_by_every_ses_parser(self) -> None:
        shapes = {
            "leading whitespace": lambda size: " " * size + "X",
            "blank lines": lambda size: "\n" * size + "X",
            "whitespace lines": lambda size: " \n" * size + "X",
        }
        for parser_name, (parse, rejected) in SES_PARSERS.items():
            for shape_name, build in shapes.items():
                with self.subTest(parser=parser_name, shape=shape_name):
                    self.assert_bounded_rejection(parsers, build, parse, rejected)

    def test_join_header_like_lines_with_long_whitespace_are_rejected_with_bounded_work(self) -> None:
        shapes = {
            "descriptor run": lambda size: "Slot01" + " " * size + "X",
            "bracket run": lambda size: "[0,1]" + " " * size + "X",
            "label run": lambda size: "Slot01 [0,1] Element type:" + " " * size,
            "after logical id": lambda size: (
                "Primary enclosure logical identifier (hex): 500000000000000a\n" + "Slot01" + " " * size + "X"
            ),
        }
        for shape_name, build in shapes.items():
            with self.subTest(shape=shape_name):
                self.assert_bounded_rejection(parsers, build, parse_sg_ses_join_filter, None)

    def test_join_element_count_ceiling_still_rejects_before_parsing(self) -> None:
        def page(count: int) -> str:
            return "\n".join(
                f"Slot{element:02d} [0,{element}]  Element type: Array device slot\n"
                f"  Predicted failure=0, Disabled=0, Swap=0, status: OK"
                for element in range(count)
            )

        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            self.assertIsNotNone(parse_sg_ses_join_filter(page(4), "sg_ses join /dev/sg7"))
            self.assertIsNone(parse_sg_ses_join_filter(page(5), "sg_ses join /dev/sg7"))

    def test_join_header_recognition_keeps_descriptor_and_bracket_rules(self) -> None:
        def only_slot(output: str):  # noqa: ANN202
            enclosure = parse_sg_ses_join_filter(
                output + "\n  Predicted failure=0, Disabled=0, Swap=0, status: OK",
                "sg_ses join /dev/sg7",
            )
            assert enclosure is not None, output
            slots = list(enclosure.slots.values()) + list(enclosure.unmapped_slots)
            self.assertEqual(len(slots), 1, output)
            return slots[0]

        cases = {
            "Slot07   [0,7]   element TYPE:   Array device slot": ("Slot07", 7),
            "[0,3] Element type: Device slot\n  device slot number: 3": ("Slot 03", 3),
            "Slot01[0,1] x [0,2] Element type: Device slot": ("Slot01[0,1] x", 2),
            "Slot02 [0,9] Element type:x [1,4] Element type: Array device slot": ("Slot02 [0,9] Element type:x", 4),
        }
        for line, (description, element_id) in cases.items():
            with self.subTest(line=line):
                slot = only_slot(line)
                self.assertEqual(slot.element_id, element_id)
                self.assertEqual(slot.description, description)

        for line in (
            "Slot01[0,1] Element type: Device slot",
            "Slot01 [0,1]Element type: Device slot",
            "Slot01 [0,1] Element type:",
            "Slot01 [0,1] Element type:Device slot",
            "Slot01 [0, 1] Element type: Device slot",
            "Slot01 [a,1] Element type: Device slot",
        ):
            with self.subTest(line=line):
                self.assertIsNone(
                    parse_sg_ses_join_filter(
                        line + "\n  Predicted failure=0, Disabled=0, Swap=0, status: OK",
                        "sg_ses join /dev/sg7",
                    )
                )

    def test_join_enclosure_name_stops_at_the_first_element_header(self) -> None:
        output = (
            "  ACME      JBOD-24      0001\n"
            "Slot01 [0,1] Element type:\n"
            "Slot02 [0,2] Element type: Array device slot\n"
            "  Predicted failure=0, Disabled=0, Swap=0, status: OK"
        )

        enclosure = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg7")

        assert enclosure is not None
        self.assertEqual(enclosure.enclosure_name, "ACME JBOD-24 0001")

        slot_bracket = parse_sg_ses_join_filter(
            "  ACME      JBOD-24      0001\nSlot01   [x\n" + output.split("\n", 1)[1],
            "sg_ses join /dev/sg7",
        )
        not_slot_bracket = parse_sg_ses_join_filter(
            "  ACME      JBOD-24      0001\nSlot01[x\n" + output.split("\n", 1)[1],
            "sg_ses join /dev/sg7",
        )

        assert slot_bracket is not None and not_slot_bracket is not None
        self.assertEqual(slot_bracket.enclosure_name, "ACME JBOD-24 0001")
        self.assertEqual(not_slot_bracket.enclosure_name, "Slot01[x")


if __name__ == "__main__":
    unittest.main()
