"""Malformed MPR and SES text is rejected with bounded parser work (#1216).

Python's regex engine exposes no step counter, so these tests count work
instead of timing it. Each regex call made by the parser module is charged the
square of the text region it was handed, which is the worst case for the
patterns these parsers use, and each Python line executed in the parser module
is charged one unit. Doubling a malformed input may at most roughly double that
count, and no regex may be handed a region that grows with the input.

A few whole-line patterns in the MPR event parser are linear by construction
(anchored, each quantifier followed by a character outside its class, ending in
`.+$`, which cannot fail on a single line). The tests name those patterns
explicitly and charge them by region instead of region squared. Any other
pattern, including a new one, is still charged as quadratic.

The later tests measure the real regex engine as a growth ratio between two
input sizes, and sweep every pattern in the parser modules (see the section
comments below).
"""

from __future__ import annotations

import ast
import asyncio
import json
import random
import re
import sys
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from re import _constants as _sre  # the parsed-pattern opcodes, for the pattern sweep
from re import _parser as _sre_parse
from unittest import mock

from pydantic import ValidationError

from app.config import SSHConfig
from app.services.ssh_probe import SSHCommandResult
from app.services import parsers
from app.services.inventory import LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, InventoryService
from app.services.parsers import (
    ParsedSSHData,
    canonicalize_ssh_command,
    normalize_device_name,
    normalize_text,
    parse_camcontrol_devlist,
    parse_glabel_status,
    parse_lsscsi_devices,
    parse_ssh_outputs,
    parse_storcli_virtual_drives,
    parse_sesutil_map,
    parse_sesutil_show_enclosures,
    parse_sg_ses_aes,
    parse_sg_ses_enclosure_status,
    parse_sg_ses_join_filter,
    parse_smartctl_text_enrichment,
    parse_unifi_gpio_debug,
)
from app.services import sas_fabric_parsers
from app.services.sas_diagnostics import scsi
from app.services.sas_diagnostics.scsi import MAX_CDB_TEXT_CHARS, decode_scsi_cdb_message
from app.services.sas_fabric_parsers import (
    _parse_mpr_sense_message,
    parse_mpr_dmesg_events,
    parse_mpr_expanders,
    parse_mpr_sysctl_locations,
    parse_pciconf_sas_controllers,
)

SIZES = (2000, 4000, 8000)
# Doubling the input of a linear pass doubles the count plus a constant.
MAX_GROWTH_PER_DOUBLING = 2.5


# Whole-line patterns of the MPR event parser that are linear by construction
# (see the module docstring). They are charged by region, not region squared.
LINEAR_MPR_LINE_PATTERNS = frozenset(
    {
        r"^\((?P<device>da\d+):(?P<controller>mpr\d+):(?P<bus>\d+):"
        r"(?P<target>\d+):(?P<lun>\d+)\):\s+(?P<message>.+)$",
        r"^\[(?P<timestamp>\s*\d+(?:\.\d+)?)\]\s+(?P<rest>.+)$",
        r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+(?P<rest>.+)$",
        r"^(?P<timestamp>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<rest>.+)$",
        r"error\s+\d+\s*,",
    }
)


class _WorkMeter:
    def __init__(self, linear_patterns: frozenset[str] = frozenset()) -> None:
        self.linear_patterns = linear_patterns
        self.regex_calls = 0
        self.regex_work = 0
        self.max_region = 0
        self.python_lines = 0

    @property
    def work(self) -> int:
        return self.regex_work + self.python_lines

    def charge(self, string: str, pos: int = 0, endpos: int | None = None, pattern: str | None = None) -> None:
        length = len(string)
        start = min(max(pos, 0), length)
        end = length if endpos is None else min(max(endpos, 0), length)
        region = max(end - start, 0)
        self.regex_calls += 1
        if pattern in self.linear_patterns:
            self.regex_work += region
            return
        self.regex_work += region * region
        self.max_region = max(self.max_region, region)


class _MeteredPattern:
    def __init__(self, pattern: re.Pattern[str], meter: _WorkMeter) -> None:
        self._pattern = pattern
        self._meter = meter

    def __getattr__(self, name: str):  # noqa: ANN204 - attribute passthrough
        return getattr(self._pattern, name)

    def search(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos, self._pattern.pattern)
        return self._pattern.search(string, pos, endpos)

    def match(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos, self._pattern.pattern)
        return self._pattern.match(string, pos, endpos)

    def fullmatch(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos, self._pattern.pattern)
        return self._pattern.fullmatch(string, pos, endpos)

    def finditer(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos, self._pattern.pattern)
        return self._pattern.finditer(string, pos, endpos)

    def findall(self, string, pos=0, endpos=sys.maxsize):  # noqa: ANN001, ANN201
        self._meter.charge(string, pos, endpos, self._pattern.pattern)
        return self._pattern.findall(string, pos, endpos)

    def sub(self, repl, string, count=0):  # noqa: ANN001, ANN201
        self._meter.charge(string, pattern=self._pattern.pattern)
        return self._pattern.sub(repl, string, count=count)

    def subn(self, repl, string, count=0):  # noqa: ANN001, ANN201
        self._meter.charge(string, pattern=self._pattern.pattern)
        return self._pattern.subn(repl, string, count=count)

    def split(self, string, maxsplit=0):  # noqa: ANN001, ANN201
        self._meter.charge(string, pattern=self._pattern.pattern)
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


def measure(
    module: types.ModuleType, call, linear_patterns: frozenset[str] = frozenset()  # noqa: ANN001
) -> tuple[object, _WorkMeter]:
    """Run `call` and count regex and Python-line work inside `module`."""

    meter = _WorkMeter(linear_patterns)
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
    def assert_bounded_rejection(self, module, build, parse, rejected, linear_patterns=frozenset()) -> None:  # noqa: ANN001
        previous_work: int | None = None
        regions: list[tuple[int, int]] = []
        for size in SIZES:
            text = build(size)
            result, meter = measure(module, lambda: parse(text), linear_patterns)
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

    def test_cdb_window_filled_by_separator_whitespace_is_not_decoded(self) -> None:
        cdb = "28 00 00 00 00 10 00 00 01 00"

        def pipeline(message: str):  # noqa: ANN202
            parsed = parse_mpr_dmesg_events("(da0:mpr0:0:1:0): " + message)
            return parsed["event_count"], dict(parsed["by_controller"]["mpr0"]["operation_counts"])

        # Controls: the normal form, the longest CDB, and separator whitespace
        # plus bytes that end exactly at the window edge all decode in full.
        longest = " ".join(["7f"] + ["00"] * 259)
        edge_spaces = MAX_CDB_TEXT_CHARS - 1 - len(cdb)
        controls = {
            "normal": (f"WRITE(10). CDB: {cdb}", 10),
            "longest": (f"VARIABLE LENGTH CDB. CDB: {longest}", 260),
            "ends at window edge": ("WRITE(10). CDB:" + " " * edge_spaces + "\t" + cdb, 10),
        }
        for name, (message, byte_count) in controls.items():
            with self.subTest(control=name):
                decoded = decode_scsi_cdb_message(message)
                self.assertEqual(decoded["byte_count"], byte_count)
                self.assertNotEqual(decoded["cdb_hex"], "")
                self.assertEqual(pipeline(message)[0], 1)
                self.assertEqual(sum(pipeline(message)[1].values()), 1)
        self.assertEqual(pipeline(controls["ends at window edge"][0]), (1, {"READ(10)": 1}))

        # The bytes start one character before, at, and one character after the
        # end of the window: the window holds only separators (or a cut-off
        # byte), so nothing is decoded and the kernel's operation name is not
        # counted on its own.
        for whitespace in (MAX_CDB_TEXT_CHARS - 1, MAX_CDB_TEXT_CHARS, MAX_CDB_TEXT_CHARS + 1):
            message = "WRITE(10). CDB:" + " " * (whitespace - 1) + "\t" + cdb
            with self.subTest(whitespace=whitespace):
                self.assertEqual(decode_scsi_cdb_message(message), {})
                self.assertEqual(pipeline(message), (1, {}))

        # An overlong separator run is refused like overlong CDB text: a later
        # "CDB:" in the same message is not decoded in its place.
        later = "WRITE(10). CDB:" + " " * MAX_CDB_TEXT_CHARS + "\tREAD(10). CDB: " + cdb
        self.assertEqual(decode_scsi_cdb_message(later), {})
        self.assertEqual(pipeline(later), (1, {}))

    def test_cdb_text_without_a_byte_token_is_not_decoded(self) -> None:
        self.assertEqual(decode_scsi_cdb_message("WRITE(10). CDB: 2a 00")["byte_count"], 2)
        for message in ("WRITE(10). CDB:  ", "WRITE(10). CDB:  \tzz", "WRITE(10). CDB:   ,2a"):
            with self.subTest(message=message):
                self.assertEqual(decode_scsi_cdb_message(message), {})
        parsed = parse_mpr_dmesg_events("(da0:mpr0:0:1:0): WRITE(10). CDB:  \tzz")
        self.assertEqual(parsed["event_count"], 1)
        self.assertEqual(dict(parsed["by_controller"]["mpr0"]["operation_counts"]), {})

    def test_cdb_text_reaching_the_window_end_is_not_decoded(self) -> None:
        cdb = "28 00 00 00 00 10 00 00 01 00"
        padding = MAX_CDB_TEXT_CHARS - 1 - len(cdb)
        control = decode_scsi_cdb_message("WRITE(10). CDB: " + cdb + " " * (padding - 1) + "\tlength 16")
        self.assertEqual(control["cdb_hex"], cdb)
        # The byte run plus trailing separators fills the window and the text
        # goes on: the end of the CDB is not inside the window.
        self.assertEqual(decode_scsi_cdb_message("WRITE(10). CDB: " + cdb + " " * padding + "\t00 00"), {})


# The patterns these parsers used before #1216, kept as the reference for the
# differential tests below.
_REFERENCE_CONTROLLER_PATTERN = re.compile(
    r"^(?P<controller>mpr\d+):\s+(?P<message>.+?)"
    r"(?:\s+tgt\s+(?P<target>\d+)\s+SMID\s+(?P<smid>\d+)\s+loginfo\s+(?P<loginfo>[0-9a-fA-F]+))?$"
)
_REFERENCE_SENSE_PATTERN = re.compile(
    r"SCSI sense:\s*(?P<sense>.+?)\s+asc:(?P<asc>[0-9a-fA-F]+,[0-9a-fA-F]+)\s+\((?P<reason>.+)\)"
)
_DIFFERENTIAL_TOKENS = (
    "mpr0:", "mpr1", ":", " ", " ", "  ", "\t", "x", "Y", "tgt", "SMID", "loginfo", "7", "12", "3a",
    "ab", "g", "SCSI sense:", "asc:", "4b,4", "11,0", ",", "(", ")", "NAK", "MEDIUM ERROR",
)


_DIFFERENTIAL_SPACES = ("", " ", " ", "  ", "\t", " \t ")
_CONTROLLER_SHAPE = ("mpr0:", " ", "Controller", " ", "x", " ", "tgt", " ", "7", " ", "SMID", " ", "12", " ", "loginfo", " ", "3a")
_SENSE_SHAPE = ("SCSI sense:", " ", "MEDIUM", " ", "ERROR", " ", "asc:", "11", ",", "0", " ", "(", "x", " ", "y", ")")


def _random_line(rng: random.Random, shape: tuple[str, ...]) -> str:
    """A line of the given shape with pieces randomly dropped, swapped or repeated."""

    pieces: list[str] = []
    for piece in shape:
        roll = rng.random()
        if roll < 0.1:
            continue
        if roll < 0.25:
            piece = rng.choice(_DIFFERENTIAL_SPACES if piece == " " else _DIFFERENTIAL_TOKENS)
        pieces.append(piece)
        if rng.random() < 0.05:
            pieces.append(rng.choice(_DIFFERENTIAL_TOKENS))
    return "".join(pieces)


def _controller_event(text: str):  # noqa: ANN202
    parsed = parse_mpr_dmesg_events(text)
    if not parsed["event_count"]:
        return None
    event = parsed["recent_events"][0]
    return event["source"], event["target"], event["smid"], event["loginfo"]


def _sense_event(text: str):  # noqa: ANN202
    event = parse_mpr_dmesg_events(text)["recent_events"][0]
    return event.get("sense_key"), event.get("asc"), event.get("reason")


class MprEventLineWorkTests(BoundedWorkAssertions):
    def assert_fabric_bounded(self, build, parse, expected) -> None:  # noqa: ANN001
        self.assert_bounded_rejection(
            sas_fabric_parsers, build, parse, expected, linear_patterns=LINEAR_MPR_LINE_PATTERNS
        )

    def test_controller_line_with_long_interior_whitespace_has_bounded_work(self) -> None:
        shapes = {
            "no fields": (lambda size: "mpr0: X" + " " * size + "Y", ("controller", None, None, None)),
            "fields": (
                lambda size: "mpr0: X" + " " * size + "Y tgt 9 SMID 4 loginfo 31120302",
                ("controller", "9", "4", "31120302"),
            ),
            "field-like words": (
                lambda size: "mpr0: X" + " tgt" * (size // 4) + " SMID 4 loginfo 3",
                ("controller", None, None, None),
            ),
        }
        for name, (build, expected) in shapes.items():
            with self.subTest(shape=name):
                self.assert_fabric_bounded(build, _controller_event, expected)

    def test_sense_message_with_long_interior_whitespace_has_bounded_work(self) -> None:
        shapes = {
            "no asc": lambda size: "(da0:mpr0:0:1:0): SCSI sense: X" + " " * size + "Y",
            "far asc": lambda size: "(da0:mpr0:0:1:0): SCSI sense: X" + " " * size + "Y asc:11,0 (Z)",
            "repeated asc": lambda size: "(da0:mpr0:0:1:0): SCSI sense: X" + " asc:1," * (size // 7),
        }
        for name, build in shapes.items():
            with self.subTest(shape=name):
                self.assert_fabric_bounded(build, _sense_event, (None, None, None))

    def test_controller_line_matches_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        cases = [
            "mpr0: Controller reported scsi ioc terminated tgt 180 SMID 142 loginfo 31120302",
            "mpr0: x  tgt 1 SMID 2 loginfo 3",
            "mpr0: tgt 1 SMID 2 loginfo 3",
            "mpr0: x\ttgt\t1\tSMID\t2\tloginfo\tab",
            "mpr0: x tgt 1 SMID 2 loginfo 3 tgt",
            "mpr0: x tgt 1 SMID 2 loginfo 3g",
            "mpr0: xtgt 1 SMID 2 loginfo 3",
            "mpr0:x",
            "mpr12: y",
            "mpr0: x tgt 1 SMID 2 loginfo 3 ",
            "mpr0:  ",
            "mpr0: ",
        ] + [_random_line(rng, _CONTROLLER_SHAPE) for _ in range(4000)]
        for line in cases:
            reference = _REFERENCE_CONTROLLER_PATTERN.match(line)
            with self.subTest(line=line):
                fields = sas_fabric_parsers._match_mpr_controller_line(line)
                if reference is None:
                    self.assertIsNone(fields)
                else:
                    assert fields is not None
                    self.assertEqual(fields["message"].strip(), reference.group("message").strip())
                    for name in ("controller", "target", "smid", "loginfo"):
                        self.assertEqual(fields[name], reference.group(name), name)

                # Through the event parser, which strips each line first.
                stripped = line.strip()
                reference = _REFERENCE_CONTROLLER_PATTERN.match(stripped)
                event = _controller_event(stripped) if stripped else None
                if reference is None:
                    self.assertIsNone(event)
                    continue
                self.assertEqual(
                    event,
                    ("controller", reference.group("target"), reference.group("smid"), reference.group("loginfo")),
                )
                self.assertEqual(
                    parse_mpr_dmesg_events(stripped)["recent_events"][0]["message"],
                    reference.group("message").strip(),
                )

    def test_sense_message_matches_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        cases = [
            "SCSI sense: ABORTED COMMAND asc:4b,4 (NAK received)",
            "SCSI sense: MEDIUM ERROR asc:11,0 (Unrecovered read error) (retry)",
            "SCSI sense: x asc:1,2 () asc:3,4 (y)",
            "SCSI sense: x asc:zz asc:3,4 (y)",
            "SCSI sense: x asc:1,2 ()",
            "SCSI sense: x asc:1,2 (",
            "SCSI sense:x  asc:1,2 (y)",
            "noise SCSI sense: x asc:1,2 (y) SCSI sense: z asc:3,4 (w)",
        ] + [_random_line(rng, _SENSE_SHAPE) for _ in range(4000)]
        for message in cases:
            reference = _REFERENCE_SENSE_PATTERN.search(message)
            with self.subTest(message=message):
                parsed = _parse_mpr_sense_message(message)
                if reference is None or not reference.group("sense").strip():
                    # An empty sense key is not a well-formed sense message.
                    self.assertEqual(parsed, {})
                    continue
                self.assertEqual(
                    parsed,
                    {
                        "sense": reference.group("sense").strip(),
                        "sense_key": reference.group("sense").strip().upper(),
                        "asc": reference.group("asc").lower(),
                        "reason": reference.group("reason").strip(),
                    },
                )

    def test_overlong_sense_message_is_not_decoded(self) -> None:
        control = _parse_mpr_sense_message("SCSI sense: ABORTED COMMAND asc:4b,4 (NAK received)")
        self.assertEqual(control["sense_key"], "ABORTED COMMAND")
        self.assertEqual(_parse_mpr_sense_message("SCSI sense:  asc:4b,4 (NAK received)"), {})
        self.assertEqual(
            _parse_mpr_sense_message("SCSI sense: " + "ABORTED COMMAND " * 30 + "asc:4b,4 (NAK received)"), {}
        )


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
            "after page title": lambda size: (
                "Enclosure Status diagnostic page:\n" + "Slot01" + " " * size + "X"
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


# --- Linear work, measured ---------------------------------------------------
#
# The work meter above models regex cost; it cannot see inside the regex
# engine. The tests below time the real engine instead, but only as a ratio:
# the same call on an input four times longer may take at most
# LINEAR_GROWTH_LIMIT times as long. Linear work grows about 4x, quadratic work
# about 16x, so the margin absorbs a busy runner. Each sample repeats the call
# for at least SAMPLE_SECONDS and keeps the fastest of three, and a ratio over
# the limit is measured again before it fails.

LINEAR_GROWTH_LIMIT = 8.0
SAMPLE_SECONDS = 0.002


def _seconds_per_call(call, text: str) -> float:  # noqa: ANN001
    best = float("inf")
    for _ in range(3):
        calls = 0
        start = time.perf_counter()
        while True:
            call(text)
            calls += 1
            elapsed = time.perf_counter() - start
            if elapsed >= SAMPLE_SECONDS:
                break
        best = min(best, elapsed / calls)
    return best


def _growth_ratio(call, small: str, large: str) -> float:  # noqa: ANN001
    return _seconds_per_call(call, large) / _seconds_per_call(call, small)


def _superlinear_growth(call, build, size: int) -> str | None:  # noqa: ANN001
    """None when the work grows linearly, else a description of the growth."""

    small, large = build(size), build(4 * size)
    ratios = []
    for _ in range(3):
        ratios.append(_growth_ratio(call, small, large))
        if ratios[-1] <= LINEAR_GROWTH_LIMIT:
            return None
    return (
        f"work grew {min(ratios):.1f}x when the input grew 4x "
        f"({len(small)} -> {len(large)} characters); linear work grows about 4x"
    )


class LinearGrowthAssertions(unittest.TestCase):
    def assert_linear_growth(self, call, build, size: int = 2000) -> None:  # noqa: ANN001
        growth = _superlinear_growth(call, build, size)
        if growth is not None:
            self.fail(growth)


# The patterns these parsers used before this change, kept as the reference
# for the exactness tests below.
_REFERENCE_EXPANDER_PHY_PATTERN = re.compile(
    r"^\s*(\d+)\s+(?:(\d+)\s+([0-9a-fA-F]{4})\s+([\d.]+)|)\s*([\d.]+|\?\?\?)\s+([\d.]+|\?\?\?)\s+(.+?)\s*$"
)
_REFERENCE_PCICONF_ATTRIBUTE_PATTERN = re.compile(r"([A-Za-z0-9_]+)=([^\s]+)")
_REFERENCE_SYSCTL_ATTRIBUTE_PATTERN = re.compile(r"([a-zA-Z_]+)=([^\s]+)")
_REFERENCE_CAMCONTROL_PATTERN = re.compile(
    r"<(?P<model>[^>]+)>.*?target\s+(?P<target>\S+)\s+lun\s+(?P<lun>\S+)\s+\((?P<devices>[^)]+)\)"
)
_REFERENCE_SATA_CURRENT_PATTERN = re.compile(r"\(current:\s*([^)]+)\)", re.IGNORECASE)
_REFERENCE_GPIO_PATTERN = re.compile(
    r"gpio-\d+\s+\(.*\|hdd@(?P<slot>\d+)\s+\)\s+(?P<direction>out|in)\s+(?P<state>hi|lo|\?)",
    re.IGNORECASE,
)
# Whitespace and digits beyond ASCII, which `\s`, `\d` and the string methods
# agree on, and no character that `str.splitlines` breaks a line at.
_EXACTNESS_SPACES = (" ", "  ", "   ", "\t", "\xa0", "\u2003", "\x1f")


def _random_text(rng: random.Random, tokens: tuple[str, ...], spaces: tuple[str, ...], length: int) -> str:
    pieces = []
    for _ in range(rng.randrange(length)):
        pieces.append(rng.choice(tokens))
        if rng.random() < 0.7:
            pieces.append(rng.choice(spaces))
    return "".join(pieces)


# A synthetic expander header; the address is built at run time.
_EXPANDER_HEADER = f"  8   {0xABC:016x}   0009   0001   0002   1"


def _expanders(*phy_lines: str):  # noqa: ANN202
    return parse_mpr_expanders("\n".join((_EXPANDER_HEADER, *phy_lines)))


class MprExpanderPhyLineWorkTests(LinearGrowthAssertions):
    def test_phy_lines_with_long_runs_have_linear_work(self) -> None:
        shapes = {
            # A long whitespace run inside the device text.
            "device tail": (
                lambda size: "  1   2   0001   12.0   12.0   12.0   X" + " " * size + "Y",
                lambda size: [("1", "2", "0001", "12.0", "12.0", "12.0", "X" + " " * size + "Y")],
            ),
            # The optional fields are absent and whitespace runs on.
            "missing speed": (lambda size: "  1" + " " * size + "X", lambda size: []),
            # A digit run where speed and min meet, with nothing after it.
            "speed digits": (
                lambda size: "  1   2   0001   " + "1" * size + "X",
                lambda size: [("1", None, None, None, "2", "0001", "1" * size + "X")],
            ),
            "min and max gap": (
                lambda size: "  1   12.0   12.0" + " " * size,
                lambda size: [("1", None, None, None, "12.0", "12.0", "")],
            ),
        }
        keys = ("phy", "remote_phy", "dev_handle", "speed", "min", "max", "device")
        for name, (build, expected) in shapes.items():
            with self.subTest(shape=name):
                parsed = _expanders(build(50))
                self.assertEqual([tuple(row[key] for key in keys) for row in parsed[0]["phys"]], expected(50))
                self.assert_linear_growth(lambda text: _expanders(text), build)

    def test_phy_lines_match_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        tokens = (
            "0", "1", "12", "12.0", "3.0", "6.0", ".", "1.", "..", "٣", "?", "??", "???", "????", "0001", "abcd",
            "ABCD", "000g", "00001", "x", "SAS", "Target", "Initiator", "No", "Device", "phy", "phy 3", "12.012.0",
            "3.0???",
        )
        cases = [
            "     0       0      000a       12     3.0    12     SAS Target",
            "    10                                3.0    12     No Device",
            "  1   2   0001   12.0   12.0   12.0  ",
            "  1   2   0001   12.0   12.0   12.0 ",
            "  1   2   0001   12.0   12.0   SAS Target",
            "  1   2   0001   12.012.0   12.0   x",
            "  1   2   0001   12.0???   12.0   x",
            "  1   2   0001   1   12.0   x",
            "  1   ???   ???   phy 4",
            "  1   ???   ???  ",
            "1 2",
        ] + [_random_text(rng, tokens, _EXACTNESS_SPACES, 12) for _ in range(6000)]
        for line in cases:
            if not line or _expanders(line)[1:]:
                continue  # empty, or another expander header
            reference = _REFERENCE_EXPANDER_PHY_PATTERN.match(line)
            expected = []
            if reference is not None and not reference.group(7).strip().lower().startswith("phy "):
                expected = [
                    {
                        "phy": reference.group(1),
                        "remote_phy": reference.group(2),
                        "dev_handle": reference.group(3),
                        "speed": reference.group(4),
                        "min": reference.group(5),
                        "max": reference.group(6),
                        "device": reference.group(7).strip(),
                    }
                ]
            with self.subTest(line=line):
                self.assertEqual(_expanders(line)[0]["phys"], expected)


class SasMetadataAttributeWorkTests(LinearGrowthAssertions):
    def test_attribute_runs_without_equals_have_linear_work(self) -> None:
        def pciconf(text: str):  # noqa: ANN202
            return parse_pciconf_sas_controllers(text)

        def sysctl(text: str):  # noqa: ANN202
            return parse_mpr_sysctl_locations(text)

        shapes = {
            "pciconf key run": (pciconf, lambda size: "mpr0@pci0:0:1:0: " + "A" * size, {}),
            "pciconf after a valid attribute": (
                pciconf,
                lambda size: "mpr0@pci0:0:1:0: class=0x010700 " + "A" * size,
                {"class_code": "0x010700"},
            ),
            "pciconf digit run": (pciconf, lambda size: "mpr0@pci0:0:1:0: " + "1" * size, {}),
            "pciconf key runs and separators": (
                pciconf,
                lambda size: "mpr0@pci0:0:1:0: " + "Ab_1 " * (size // 5),
                {},
            ),
            "sysctl key run": (sysctl, lambda size: "dev.mpr.0.%location: " + "A" * size, {}),
            "sysctl after a valid attribute": (
                sysctl,
                lambda size: "dev.mpr.0.%location: slot=1 " + "a" * size,
                {"pci_slot": "1"},
            ),
        }
        for name, (parse, build, attributes) in shapes.items():
            with self.subTest(shape=name):
                parsed = parse(build(50))
                row = parsed[0] if isinstance(parsed, list) else parsed["mpr0"]
                for key, value in attributes.items():
                    self.assertEqual(row[key], value)
                self.assert_linear_growth(parse, build)

    def test_attributes_match_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        tokens = ("a", "A", "z", "_", "0", "9", "=", "==", "x=", "=y", "class=0x01", "dbsf=pci0:0:1:0", ".", "-", "é")
        cases = ["slot=1 function=0 dbsf=pci0:3:0:0 handle=\\_SB_.PCI0", "a=b=c d= =e f==g", "1a=2 _=_"] + [
            _random_text(rng, tokens, (" ", "\t", "\xa0", ""), 10) for _ in range(6000)
        ]
        pairs = (
            (sas_fabric_parsers._PCICONF_ATTRIBUTE_PATTERN, _REFERENCE_PCICONF_ATTRIBUTE_PATTERN),
            (sas_fabric_parsers._SYSCTL_LOCATION_ATTRIBUTE_PATTERN, _REFERENCE_SYSCTL_ATTRIBUTE_PATTERN),
        )
        for text in cases:
            for pattern, reference in pairs:
                with self.subTest(text=text, pattern=pattern.pattern):
                    self.assertEqual(pattern.findall(text), reference.findall(text))


class ParserLineWorkTests(LinearGrowthAssertions):
    def test_camcontrol_device_lines_have_linear_work(self) -> None:
        tail = " at scbus0 target 0 lun 0 (pass0,da0)"
        shapes = {
            "models without a target": (lambda size: "<a>" * (size // 3), {}),
            "unclosed model": (lambda size: "<" * size, {}),
            "target fields without a device list": (
                lambda size: "<a> " + "target 1 lun 2 (" * (size // 16),
                {},
            ),
            "match after many models": (lambda size: "<a>" * (size // 3) + tail, {"da0": "a"}),
            "open brackets then a match": (lambda size: "<>" * (size // 2) + "<disk>" + tail, {"da0": "disk"}),
        }
        for name, (build, models) in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(parse_camcontrol_devlist(build(60)).models, models)
                self.assert_linear_growth(parse_camcontrol_devlist, build)

    def test_camcontrol_device_lines_match_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        tokens = (
            "<", ">", "<>", "<a>", "<disk 1>", "target", "lun", "0", "12", "(", ")", "()", "(da0)", "(pass0,da0)",
            "at", "scbus0", "x", ">target",
        )
        cases = [
            "<SEAGATE ST4000NM0023 0004>        at scbus0 target 0 lun 0 (pass0,da0)",
            "<>  <a> target 1 lun 2 (da1)",
            "<a> target 1 lun 2 () target 3 lun 4 (da3)",
            "<a> target 1 lun 2 (da1) <b> target 3 lun 4 (da3)",
            "<a target 1 lun 2 (da1)",
            "<a> target 1 lun 2 (da1",
        ] + [_random_text(rng, tokens, (" ", "  ", "\t", ""), 14) for _ in range(6000)]
        for line in cases:
            reference = _REFERENCE_CAMCONTROL_PATTERN.search(line)
            match = parsers._match_camcontrol_device_line(line)
            with self.subTest(line=line):
                if reference is None:
                    self.assertIsNone(match)
                    continue
                assert match is not None
                self.assertEqual(match.span(), reference.span())
                self.assertEqual(match.groupdict(), reference.groupdict())

    def test_sata_current_link_rate_has_linear_work(self) -> None:
        def parse(text: str):  # noqa: ANN202
            return parse_smartctl_text_enrichment("SATA Version is: " + text)

        shapes = {
            "whitespace without a close": (lambda size: "SATA 3.3, 6.0 Gb/s (current:" + " " * size + "x", None),
            "repeated opens": (lambda size: "SATA 3.3 " + "(current:x" * (size // 10), None),
            "close at the end": (lambda size: "SATA 3.3 (current:" + " " * size + "x)", "x"),
        }
        for name, (build, rate) in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(parse(build(60)).get("negotiated_link_rate"), rate)
                self.assert_linear_growth(parse, build)

    def test_sata_current_link_rate_matches_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        tokens = ("(current:", "(CURRENT:", "(", ")", "6.0 Gb/s", "x", ",", "SATA 3.3", ")(current:")
        cases = ["SATA 3.3, 6.0 Gb/s (current: 6.0 Gb/s)", "(current: )", "(current:)", "(current:  ) x"] + [
            _random_text(rng, tokens, (" ", "  ", "\t", ""), 8) for _ in range(6000)
        ]
        for value in cases:
            reference = _REFERENCE_SATA_CURRENT_PATTERN.search(value)
            match = parsers._SATA_CURRENT_LINK_RATE_PATTERN.search(value, 0, value.rfind(")") + 1)
            with self.subTest(value=value):
                self.assertEqual(match and (match.span(), match.groups()), reference and (reference.span(), reference.groups()))
                parsed = parse_smartctl_text_enrichment("SATA Version is: " + value)
                self.assertEqual(
                    parsed.get("negotiated_link_rate"),
                    reference and normalize_text(reference.group(1)),
                )

    def test_unifi_gpio_lines_have_linear_work(self) -> None:
        line = " gpio-500 (                    |hdd@3               ) out hi"
        shapes = {
            "opens without a bay": (lambda size: " gpio-1 (" * (size // 9), {}),
            "match after many opens": (lambda size: " gpio-1 (" * (size // 9) + line, {3: True}),
            "bays without a direction": (lambda size: " gpio-1 (" + "|hdd@1 )" * (size // 8), {}),
        }
        for name, (build, states) in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(parse_unifi_gpio_debug(build(90)), states)
                self.assert_linear_growth(parse_unifi_gpio_debug, build)

    def test_unifi_gpio_lines_match_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        tokens = ("gpio-1", "GPIO-500", "gpio-", "(", ")", "|hdd@", "|hdd@3", "3", "out", "in", "hi", "lo", "?", "|")
        cases = [
            " gpio-500 (                    |hdd@0               ) out hi",
            " gpio-1 ( |hdd@1 ) out lo gpio-2 ( |hdd@2 ) in hi",
            " gpio-1 ( |hdd@1 ) out x |hdd@2 ) in hi",
        ] + [_random_text(rng, tokens, (" ", "  ", "\t", ""), 14) for _ in range(6000)]
        for line in cases:
            reference = _REFERENCE_GPIO_PATTERN.search(line)
            with self.subTest(line=line):
                expected = {}
                if reference is not None and reference.group("direction").lower() == "out":
                    state = reference.group("state").lower()
                    if state in {"hi", "lo"}:
                        expected = {int(reference.group("slot")): state == "hi"}
                self.assertEqual(parse_unifi_gpio_debug(line), expected)


class GlabelStatusWorkTests(LinearGrowthAssertions):
    def test_many_labels_on_one_provider_have_linear_work(self) -> None:
        def build(size: int) -> str:
            return "\n".join(f"gptid/label-{index}  N/A  da0p{index % 4 + 1}" for index in range(size // 30))

        info = parse_glabel_status(build(300) + "\ngptid/label-0  N/A  da0p1")
        self.assertEqual(info.device_to_gptids["da0"], [f"gptid/label-{index}" for index in range(10)])
        self.assert_linear_growth(parse_glabel_status, build, size=60000)


class SgSesJoinFirstHeaderTests(unittest.TestCase):
    def test_first_of_two_valid_headers_supplies_the_element(self) -> None:
        line = "Slot01 [0,1] Element type: Device slot [0,2] Element type: Array device slot"

        self.assertEqual(
            parsers._match_sg_ses_join_element_header(line),
            ("Slot01", "0", "1", "Device slot [0,2] Element type: Array device slot"),
        )
        # The first header's type is the rest of the line, which is not a
        # device slot, so no slot comes from this line; the second header
        # would have made element 2 a slot.
        self.assertIsNone(
            parse_sg_ses_join_filter(
                line + "\n  Predicted failure=0, Disabled=0, Swap=0, status: OK",
                "sg_ses join /dev/sg7",
            )
        )


# --- Parsers whose work grew with a second dimension of the input -------------
#
# Each test below pairs a growth case (the shared n-against-4n helper) with a
# control that ordinary output still parses to the same result.

_REFERENCE_LSSCSI_TRANSPORT_PATTERN = re.compile(r"\b(?P<transport>[A-Za-z0-9_+-]+):(?P<address>\S+)")


def _lsscsi_transport(text: str):  # noqa: ANN202
    devices = parse_lsscsi_devices(text, transport=True)
    return devices[0].transport, devices[0].transport_address


class LsscsiTransportWorkTests(LinearGrowthAssertions):
    def test_transport_text_without_a_colon_has_linear_work(self) -> None:
        head = "[0:0:0:0]    disk    "
        shapes = {
            # Word and punctuation characters alternate, so every word starts
            # at a new boundary inside one run of transport characters.
            "word and dash": (lambda size: head + "a-" * (size // 2) + "!", (None, None)),
            "mixed word and punctuation": (lambda size: head + "ab+_-9-" * (size // 7) + " /dev/sda", (None, None)),
            "dash then word": (lambda size: head + "-a" * (size // 2), (None, None)),
            "run then a colon and no address": (lambda size: head + "a-" * (size // 2) + ":", (None, None)),
            "run then a transport": (
                lambda size: head + "a-" * (size // 2) + "! sas:0x12ab /dev/sda /dev/sg0",
                ("sas", "0x12ab"),
            ),
        }
        for name, (build, expected) in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(_lsscsi_transport(build(40)), expected)
                self.assert_linear_growth(_lsscsi_transport, build)

    def test_transport_matches_the_previous_pattern(self) -> None:
        rng = random.Random(1216)
        tokens = ("a", "Z", "9", "_", "-", "+", "-a", "a-", ":", "::", "sas:", "0x5", "é", "é-", "٣", "!", ",", ";", "/", ".")
        cases = [
            "sas:0x12ab",
            "fc:0x12ab,0x34cd",
            "sata:",
            "iqn.2001-04.com.example:storage,t,0x1",
            "-sas:x",
            "+-:x",
            "é-sas:x",
            "a-b-c:d",
            "x sas:  y:z",
        ] + [_random_text(rng, tokens, (" ", "  ", "\t", "\xa0", ""), 10) for _ in range(6000)]
        for detail in cases:
            reference = _REFERENCE_LSSCSI_TRANSPORT_PATTERN.search(detail)
            match = parsers._search_lsscsi_transport(detail)
            with self.subTest(detail=detail):
                self.assertEqual(
                    match and (match.span(), match.groupdict()),
                    reference and (reference.span(), reference.groupdict()),
                )
        # Through the parser, with a device path and a sg path on the line.
        self.assertEqual(
            _lsscsi_transport("[1:0:2:0]  disk  sas:0x12ab,  /dev/sdb  /dev/sg1"),
            ("sas", "0x12ab"),
        )


def _reference_camcontrol_peers(output: str) -> dict[str, list[str]]:
    """The alias expansion `parse_camcontrol_devlist` did before this change."""

    peers: dict[str, list[str]] = {}
    for line in output.splitlines():
        match = _REFERENCE_CAMCONTROL_PATTERN.search(line)
        if not match:
            continue
        devices = [
            normalized
            for device in match.group("devices").split(",")
            if parsers.DEVICE_REGEX.search(device.strip()) and (normalized := normalize_device_name(device))
        ]
        deduped = list(dict.fromkeys(devices))
        if len(deduped) < 2:
            continue
        for device in deduped:
            others = [peer for peer in deduped if peer.lower() != device.lower()]
            if others:
                peers[device.lower()] = others
    return peers


class CamcontrolAliasGroupTests(LinearGrowthAssertions):
    def test_many_aliases_on_one_row_have_linear_work(self) -> None:
        def build(size: int) -> str:
            aliases = ",".join(f"da{index}" for index in range(size // 6))
            return f"<SYNTH DISK 0001>  at scbus0 target 0 lun 0 ({aliases})"

        info = parse_camcontrol_devlist(build(60))
        self.assertEqual(list(info.peer_devices["da0"]), [f"da{index}" for index in range(1, 10)])
        self.assertEqual(list(info.peer_devices["da9"]), [f"da{index}" for index in range(9)])
        self.assert_linear_growth(parse_camcontrol_devlist, build, size=6000)

    def test_peer_devices_match_the_previous_expansion(self) -> None:
        rng = random.Random(1216)
        aliases = ("da0", "DA0", "da1", "ada0", "ada1", "pass0", "nvd0", "da0p1", "sd0", "x", "", " da1 ")
        lines = [
            "scbus0 on mpr0 bus 0:",
            "<SYNTH DISK 0001>  at scbus0 target 0 lun 0 (pass0,da0)",
            "<SYNTH DISK 0002>  at scbus0 target 1 lun 0 (da1,ada1,pass1)",
            "<SYNTH DISK 0003>  at scbus0 target 2 lun 0 (da0,DA0)",
            "<SYNTH DISK 0004>  at scbus0 target 3 lun 0 (da2)",
        ]
        for _ in range(3000):
            row = ",".join(rng.choice(aliases) for _ in range(rng.randrange(1, 6)))
            lines.append(f"<SYNTH DISK {rng.randrange(9)}>  at scbus0 target {rng.randrange(4)} lun 0 ({row})")
        for start in range(0, len(lines), 10):
            output = "\n".join(lines[start : start + 10])
            peers = parse_camcontrol_devlist(output).peer_devices
            with self.subTest(output=output):
                self.assertEqual({key: list(value) for key, value in peers.items()}, _reference_camcontrol_peers(output))
                # The values still compare equal to the lists they replace.
                self.assertEqual(peers, _reference_camcontrol_peers(output))


def _storcli_virtual_drives(response: dict) -> list[dict]:
    return parse_storcli_virtual_drives(json.dumps({"Controllers": [{"Response Data": response}]}))


def _reference_storcli_pd_slots(response: dict, vd_id: str) -> list[str]:
    """The PD rows the parser associated with one VD before this change."""

    rows = []
    for key, value in response.items():
        if re.search(rf"\bPDs\s+for\s+VD\s+{re.escape(vd_id)}\b", key, re.IGNORECASE) and isinstance(value, list):
            rows.extend(item["EID:Slt"] for item in value if isinstance(item, dict))
    return rows


class StorcliVirtualDriveWorkTests(LinearGrowthAssertions):
    def test_many_virtual_drives_and_sections_have_linear_work(self) -> None:
        def build(size: int) -> dict:
            count = size // 16
            response = {"VD LIST": [{"DG/VD": f"0/{index}", "Name": "x"} for index in range(count)]}
            response.update({f"PDs for VD {index}": [{"EID:Slt": f"252:{index}"}] for index in range(count)})
            return response

        drives = _storcli_virtual_drives(build(160))
        self.assertEqual([drive["vd_id"] for drive in drives], [str(index) for index in range(10)])
        self.assertEqual([[pd["slot_key"] for pd in drive["physical_drives"]] for drive in drives][3], ["252:3"])
        self.assert_linear_growth(lambda response: _storcli_virtual_drives(response), build)

    def test_physical_drive_sections_match_the_previous_association(self) -> None:
        rng = random.Random(1216)
        headings = (
            "PDs for VD {}", "pds  for\tvd {}", "PDS FOR VD  {}", "PDs for VD {}x", "PDs for VD {}_1", "PDs for VD {}-a",
            "PDs for VD {}/1", "xPDs for VD {}", "PDs for VD 0{}", "PDs for VD {} PDs for VD 1", "PDs forVD {}",
            "VD{} Properties", "PDs for VD {}\u00e9", "PDs for VD {}\u0663", "/c0/v{}",
        )
        vd_ids = ("0", "1", "2", "10", "01", "00")
        for trial in range(400):
            response: dict = {
                "VD LIST": [{"DG/VD": f"0/{vd_id}", "Name": "x"} for vd_id in rng.sample(vd_ids, rng.randrange(1, 4))]
            }
            for index in range(rng.randrange(1, 6)):
                key = rng.choice(headings).format(rng.choice(vd_ids))
                response.setdefault(key, []).append({"EID:Slt": f"252:{trial % 50 + index}"})
            with self.subTest(response=response):
                for drive in _storcli_virtual_drives(response):
                    self.assertEqual(
                        [pd["slot_key"] for pd in drive["physical_drives"]],
                        _reference_storcli_pd_slots(response, drive["vd_id"]),
                    )

    def test_a_row_without_a_virtual_drive_number_is_not_given_every_section(self) -> None:
        # Behaviour choice: a row whose VD number is missing or not decimal is
        # not associated with any "PDs for VD" section (it used to collect all
        # of them, which is a guess).
        response = {
            "VD LIST": [{"Name": "no number"}, {"DG/VD": "0/x", "Name": "word"}, {"DG/VD": "0/0", "Name": "control"}],
            "PDs for VD 0": [{"EID:Slt": "252:0"}],
            "PDs for VD 1": [{"EID:Slt": "252:1"}],
        }
        drives = _storcli_virtual_drives(response)
        self.assertEqual([[pd["slot_key"] for pd in drive["physical_drives"]] for drive in drives], [[], [], ["252:0"]])


class SshCommandNormalizationWorkTests(LinearGrowthAssertions):
    def test_many_sudo_options_have_linear_work(self) -> None:
        shapes = {
            "repeated option": lambda size: "sudo " + "-n " * (size // 3) + "glabel status",
            "repeated sudo": lambda size: "sudo -n " * (size // 8) + "glabel status",
        }
        for name, build in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(canonicalize_ssh_command(build(300)), "glabel status")
                # shlex's own linear cost hides the prefix loop at sizes a test
                # can afford, so the words come from `str.split` here; quoting
                # plays no part in these inputs.
                with mock.patch.object(parsers.shlex, "split", str.split):
                    self.assertEqual(canonicalize_ssh_command(build(300)), "glabel status")
                    self.assert_linear_growth(canonicalize_ssh_command, build, size=30000)
        for command, key in (
            ("sudo -n glabel status", "glabel status"),
            ("sudo -n /sbin/camcontrol devlist -v", "camcontrol devlist -v"),
            ("sudo", "sudo"),
            ("sudo -n", "sudo -n"),
        ):
            with self.subTest(command=command):
                self.assertEqual(canonicalize_ssh_command(command), key)

    def test_configured_commands_are_bounded_at_load(self) -> None:
        self.assertEqual(len(SSHConfig().commands), 6)
        SSHConfig(commands=["x" * 4096, " ".join(["x"] * 256), "sudo -n /usr/bin/sg_ses -p aes /dev/sg3"])
        for command, message in (
            ("x" * 4097, "An SSH command is longer than 4096 characters."),
            (" ".join(["secret"] * 257), "An SSH command has more than 256 words."),
        ):
            with self.subTest(length=len(command)):
                with self.assertRaises(ValidationError) as caught:
                    SSHConfig(commands=["glabel status", command])
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("xxxx", str(caught.exception))


_PARITY_FIXTURES = Path(__file__).parent / "fixtures" / "platform_parity"


def _overlay_stub(slot_count: int, *, merge: bool = True) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(layout=types.SimpleNamespace(slot_count=slot_count)),
        _optional_ssh_transport_failure_detail=InventoryService._optional_ssh_transport_failure_detail,
        _tag_ses_overlay=InventoryService._tag_ses_overlay if merge else (lambda parsed, host: None),
        _merge_ses_overlay_data=InventoryService._merge_ses_overlay_data if merge else (lambda base, overlay: base),
    )


def _overlay_results(devices: list[str], pages: dict[str, str], sysfs: str) -> list[SSHCommandResult]:
    results = []
    for device in devices:
        for page, text in pages.items():
            command = (
                f"sudo -n /usr/bin/sg_ses --join --filter {device}"
                if page == "join"
                else f"sudo -n /usr/bin/sg_ses -p {page} {device}"
            )
            results.append(SSHCommandResult(command=command, ok=True, stdout=text, stderr="", exit_code=0))
    results.append(
        SSHCommandResult(command=LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, ok=True, stdout=sysfs, stderr="", exit_code=0)
    )
    return results


def _fetch_overlay(stub, devices: list[str], results: list[SSHCommandResult]):  # noqa: ANN001, ANN202
    return asyncio.run(
        InventoryService._fetch_sg_ses_host_overlay(
            stub, "host-a", devices, failure_prefix="SES", command_results=results
        )
    )


class SesOverlayHostSysfsTests(LinearGrowthAssertions):
    def test_host_sysfs_map_is_parsed_once_for_all_devices(self) -> None:
        aes = (_PARITY_FIXTURES / "scale_offset_1based_aes.txt").read_text(encoding="utf-8")
        sysfs_line = (_PARITY_FIXTURES / "scale_offset_1based_sysfs.txt").read_text(encoding="utf-8")

        def build(size: int):  # noqa: ANN202
            devices = [f"/dev/sg{index}" for index in range(size)]
            sysfs = "".join(sysfs_line.replace("sg9 ", f"sg{index} ") for index in range(size))
            return devices, _overlay_results(devices, {"aes": aes}, sysfs)

        stub = _overlay_stub(4, merge=False)
        devices, results = build(32)
        consumed = []
        real = parsers.parse_enclosure_sysfs_map
        with mock.patch.object(parsers, "parse_enclosure_sysfs_map", lambda text: consumed.append(len(text)) or real(text)):
            with mock.patch("app.services.inventory.parse_enclosure_sysfs_map", parsers.parse_enclosure_sysfs_map):
                _fetch_overlay(_overlay_stub(4), devices, results)
        sysfs_length = len(results[-1].stdout)
        self.assertEqual(sum(consumed), sysfs_length)
        self.assert_linear_growth(lambda built: _fetch_overlay(stub, *built), build, size=32)

    def test_overlay_is_unchanged_for_ordinary_output(self) -> None:
        pages = {
            page: (_PARITY_FIXTURES / f"scale_offset_1based_{page}.txt").read_text(encoding="utf-8")
            for page in ("aes", "ec", "join")
        }
        sysfs = (_PARITY_FIXTURES / "scale_offset_1based_sysfs.txt").read_text(encoding="utf-8")
        devices = ["/dev/sg9", "/dev/sg10"]
        results = _overlay_results(devices, pages, sysfs)
        overlay, failures = _fetch_overlay(_overlay_stub(4), devices, results)

        # The same overlay built the way the caller did before this change:
        # each device's pages parsed together with the whole host map.
        expected = ParsedSSHData()
        by_command = {result.command: result.stdout for result in results}
        for device in devices:
            outputs = {command: text for command, text in by_command.items() if command.endswith(f" {device}")}
            outputs[LINUX_ENCLOSURE_SYSFS_MAP_COMMAND] = sysfs
            parsed = parse_ssh_outputs(outputs, 4, None, None)
            if parsed.ses_enclosures:
                InventoryService._tag_ses_overlay(parsed, "host-a")
                expected = InventoryService._merge_ses_overlay_data(expected, parsed)

        self.assertEqual(failures, [])
        self.assertTrue(overlay.ses_enclosures)
        self.assertEqual(overlay.ses_enclosures, expected.ses_enclosures)
        self.assertEqual(overlay.ses_slot_candidates, expected.ses_slot_candidates)
        self.assertEqual(overlay.ses_slot_to_device, expected.ses_slot_to_device)
        # The host map still names the disks in the bays its device reads.
        for bay, device in {0: "sda", 1: "sdb", 3: "sdd"}.items():
            self.assertEqual(overlay.ses_slot_candidates[bay].get("device_names"), [device])
        self.assertFalse(overlay.ses_slot_candidates[2].get("device_names"))


# --- Every pattern in the parser modules -------------------------------------
#
# The modules this suite covers are read from source: every `re` call and every
# compiled pattern, with the method each one is used with. Every quantifier
# that can repeat without bound is then "pumped": the shortest text that reaches
# it, a long run of one character the quantifier accepts (or of the text that
# reaches it, so that every start of a search is tried too), and an ending that
# makes the rest of the pattern fail. Each pumped text is run at two lengths
# through the pattern's own method, or through the function that bounds it
# where a pattern is only linear at its call site. A pattern added later is
# swept the same way without editing this test.

SWEPT_MODULES = (parsers, sas_fabric_parsers, scsi)
_RE_FUNCTIONS = frozenset({"compile", "match", "search", "fullmatch", "findall", "finditer", "sub", "subn", "split"})
_PATTERN_METHODS = frozenset(_RE_FUNCTIONS - {"compile"})
_FLAG_POSITIONS = {"compile": 1, "match": 2, "search": 2, "fullmatch": 2, "findall": 2, "finditer": 2, "sub": 4,
                   "subn": 4, "split": 3}
# Functions that compile a pattern their callers pass in: (argument position,
# flags, method).
_PATTERNS_FROM_CALLERS = {"_ses_input_exceeds_bounds": (1, re.IGNORECASE, "match")}
# Patterns that do more than linear work under an unanchored search, so their
# callers bound them. Each one is pumped through that caller instead of the
# bare pattern, and each has its own named tests above.
_CALL_SITE_BOUNDED = {
    r"(?P<operation>[A-Z][A-Z0-9 _/-]*(?:\([A-Z0-9]+\))?)\.\s+CDB:\s+(?P<cdb>[0-9a-fA-F ]+)": (
        decode_scsi_cdb_message,
        "matched only inside the windows found around CDB:",
    ),
    r"[A-Z][A-Z0-9 _/-]*(?:\([A-Z0-9]+\))?\.\s+\Z": (
        decode_scsi_cdb_message,
        "searched only in the window before CDB:",
    ),
    r"CDB:\s+[0-9a-fA-F][0-9a-fA-F ]*": (decode_scsi_cdb_message, "matched only in the window after CDB:"),
    r"SCSI sense:\s*(?P<sense>\S.*?)\s+asc:(?P<asc>[0-9a-fA-F]+,[0-9a-fA-F]+)\s+\(": (
        _parse_mpr_sense_message,
        "matched only in the window after SCSI sense:",
    ),
    r"<(?P<model>[^>]+)>.*?target\s+(?P<target>\S+)\s+lun\s+(?P<lun>\S+)\s+\((?P<devices>[^)]+)\)": (
        parse_camcontrol_devlist,
        "matched once at the first model, up to the last )",
    ),
    r"\(current:\s*([^)]+)\)": (
        lambda text: parse_smartctl_text_enrichment("SATA Version is: " + text),
        "searched only up to the last )",
    ),
    r"gpio-\d+\s+\(.*\|hdd@(?P<slot>\d+)\s+\)\s+(?P<direction>out|in)\s+(?P<state>hi|lo|\?)": (
        parse_unifi_gpio_debug,
        "matched once at the first gpio-N (",
    ),
}
_PUMP_CHARACTERS = tuple(" \t\xa0aA0f9_.,:=-/x()[]?<>;#%+*'\"zZ!@|\n\x00é٣")
_PUMP_ENDINGS = ("", "\x00", "!", "\n!", "x\x00")
_PUMP_SIZE = 1000
# Screening uses one fastest-of-three call per length. Below this many seconds
# at the longer length a ratio is timer noise: quadratic work at that length
# takes far longer. Anything over the limit is measured again, in full.
_SCREEN_FLOOR_SECONDS = 0.001


@dataclass(frozen=True)
class _RegexSite:
    module: str
    line: int
    pattern: str
    flags: int
    methods: frozenset[str]


def _flag_value(node: ast.expr) -> int:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re":
        return int(getattr(re, node.attr))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flag_value(node.left) | _flag_value(node.right)
    raise AssertionError(f"unrecognized regex flags: {ast.dump(node)}")


def _literal_pattern(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # An escaped value interpolated into the pattern: a literal word.
        return "".join(part.value if isinstance(part, ast.Constant) else "Field" for part in node.values)
    return None


def _re_call(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
        and node.func.attr in _RE_FUNCTIONS
    ):
        return node.func.attr
    return None


def _method_calls(scope: ast.AST, name: str) -> frozenset[str]:
    return frozenset(
        node.func.attr
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == name
        and node.func.attr in _PATTERN_METHODS
    )


def _regex_sites(module: types.ModuleType) -> list[_RegexSite]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    name = module.__name__
    # The innermost function around each node: ast.walk visits outer
    # functions first, so inner ones overwrite them.
    owners: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(function):
                owners[id(child)] = function
    assigned: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned[id(node.value)] = node.targets[0].id

    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _PATTERNS_FROM_CALLERS:
            position, flags, method = _PATTERNS_FROM_CALLERS[node.func.id]
            pattern = _literal_pattern(node.args[position])
            if pattern is not None:
                sites.append(_RegexSite(name, node.lineno, pattern, flags, frozenset({method})))
            continue
        function = _re_call(node)
        if function is None:
            continue
        pattern = _literal_pattern(node.args[0])
        if pattern is None:
            owner = owners.get(id(node))
            if owner is not None and owner.name in _PATTERNS_FROM_CALLERS:
                continue  # swept at each caller above
            raise AssertionError(f"{name}:{node.lineno}: a pattern that is not a literal cannot be swept")
        flags_node = next((keyword.value for keyword in node.keywords if keyword.arg == "flags"), None)
        if flags_node is None and len(node.args) > _FLAG_POSITIONS[function]:
            flags_node = node.args[_FLAG_POSITIONS[function]]
        flags = _flag_value(flags_node) if flags_node is not None else 0
        if function == "compile":
            bound = assigned.get(id(node))
            scope = owners.get(id(node), tree)
            methods = _method_calls(scope, bound) if bound else frozenset()
            if bound is None or not methods or (scope is tree and not bound.startswith("_")):
                # Unknown or shared use: sweep it as an unanchored search too.
                methods |= {"search"}
        else:
            methods = frozenset({function})
        sites.append(_RegexSite(name, node.lineno, pattern, flags, methods))
    return sites


def _class_member(item: tuple, character: str, flags: int) -> bool:
    op, value = item
    if op is _sre.LITERAL:
        return re.fullmatch(re.escape(chr(value)), character, flags) is not None
    if op is _sre.NOT_LITERAL:
        return re.fullmatch(re.escape(chr(value)), character, flags) is None
    if op is _sre.ANY:
        return character != "\n" or bool(flags & re.DOTALL)
    if op is _sre.IN:
        parts = []
        for member_op, member in value:
            if member_op is _sre.NEGATE:
                parts.insert(0, "^")
            elif member_op is _sre.LITERAL:
                parts.append(re.escape(chr(member)))
            elif member_op is _sre.RANGE:
                parts.append(f"{re.escape(chr(member[0]))}-{re.escape(chr(member[1]))}")
            elif member_op is _sre.CATEGORY:
                parts.append(_CATEGORY_CLASSES[member])
            else:
                raise AssertionError(f"unsupported class member {member_op}")
        return re.fullmatch("[" + "".join(parts) + "]", character, flags) is not None
    return False


_CATEGORY_CLASSES = {
    _sre.CATEGORY_DIGIT: r"\d",
    _sre.CATEGORY_NOT_DIGIT: r"\D",
    _sre.CATEGORY_SPACE: r"\s",
    _sre.CATEGORY_NOT_SPACE: r"\S",
    _sre.CATEGORY_WORD: r"\w",
    _sre.CATEGORY_NOT_WORD: r"\W",
}
_SINGLE_CHARACTER_OPS = (_sre.LITERAL, _sre.NOT_LITERAL, _sre.ANY, _sre.IN)
_REPEAT_OPS = (_sre.MAX_REPEAT, _sre.MIN_REPEAT, _sre.POSSESSIVE_REPEAT)


def _shortest_text(items, flags: int) -> str:  # noqa: ANN001
    """A short text that the parsed items accept, ignoring anchors and lookarounds."""

    text = []
    for op, value in items:
        if op in _SINGLE_CHARACTER_OPS:
            candidates = _PUMP_CHARACTERS + tuple(chr(code) for code in range(32, 127))
            text.append(next(character for character in candidates if _class_member((op, value), character, flags)))
        elif op in _REPEAT_OPS:
            text.append(_shortest_text(value[2], flags) * value[0])
        elif op is _sre.SUBPATTERN:
            text.append(_shortest_text(value[-1], flags))
        elif op is _sre.ATOMIC_GROUP:
            text.append(_shortest_text(value, flags))
        elif op is _sre.BRANCH:
            text.append(_shortest_text(value[1][0], flags))
        elif op not in (_sre.AT, _sre.ASSERT, _sre.ASSERT_NOT):
            raise AssertionError(f"unsupported regex construct {op}")
    return "".join(text)


def _pump_points(items, flags: int, before: str = "", after: str = ""):  # noqa: ANN001, ANN202
    """(text before, repeated unit, text after) for every unbounded quantifier."""

    items = list(items)
    for index, (op, value) in enumerate(items):
        prefix = before + _shortest_text(items[:index], flags)
        suffix = _shortest_text(items[index + 1 :], flags) + after
        if op in _REPEAT_OPS:
            body = list(value[2])
            if value[1] is _sre.MAXREPEAT or value[1] > 64:
                if len(body) == 1 and body[0][0] in _SINGLE_CHARACTER_OPS:
                    units = [character for character in _PUMP_CHARACTERS if _class_member(body[0], character, flags)]
                else:
                    units = [_shortest_text(body, flags) or "x"]
                for unit in units:
                    yield prefix, unit, suffix
                    if prefix:
                        yield "", prefix + unit, suffix
            yield from _pump_points(body, flags, prefix, suffix)
        elif op is _sre.SUBPATTERN:
            yield from _pump_points(value[-1], flags, prefix, suffix)
        elif op is _sre.ATOMIC_GROUP:
            yield from _pump_points(value, flags, prefix, suffix)
        elif op is _sre.BRANCH:
            for alternative in value[1]:
                yield from _pump_points(alternative, flags, prefix, suffix)
        elif op in (_sre.ASSERT, _sre.ASSERT_NOT):
            yield from _pump_points(value[1], flags, prefix, suffix)


def _pumped_texts(pattern: str, flags: int):  # noqa: ANN202
    """(builder of a text of about `size` characters, label) for every pump."""

    seen = set()
    for prefix, unit, suffix in _pump_points(_sre_parse.parse(pattern, flags).data, flags):
        for ending in (*_PUMP_ENDINGS, suffix, suffix + "\x00"):
            key = (prefix, unit, ending)
            if key in seen:
                continue
            seen.add(key)
            yield (
                lambda size, prefix=prefix, unit=unit, ending=ending: (
                    prefix + unit * max(1, size // len(unit)) + ending
                ),
                f"{prefix[:30]!r} + {unit[:30]!r} * n + {ending[:30]!r}",
            )


def _pattern_call(pattern: re.Pattern[str], method: str):  # noqa: ANN202
    if method in {"sub", "subn"}:
        return lambda text: getattr(pattern, method)("", text)
    return getattr(pattern, method)


def _fastest_call(call, text: str) -> float:  # noqa: ANN001
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        call(text)
        best = min(best, time.perf_counter() - start)
    return best


class ParserPatternSweepTests(LinearGrowthAssertions):
    maxDiff = None

    def test_every_pattern_in_the_parser_modules_is_swept(self) -> None:
        sites = [site for module in SWEPT_MODULES for site in _regex_sites(module)]
        patterns = {site.pattern for site in sites}

        # The sweep found the patterns this suite already knows about, so the
        # source reading works; and every call-site exception still exists.
        self.assertGreater(len(sites), 100)
        for known in (r"[^a-z0-9]+", r"^\s*Element\s+\d+,\s+Type:", r"(?:slot|bay|element)\D{0,4}(?P<slot>\d{1,3})"):
            self.assertIn(known, patterns)
        self.assertEqual(set(_CALL_SITE_BOUNDED) - patterns, set())
        for site in sites:
            with self.subTest(site=f"{site.module}:{site.line}"):
                self.assertTrue(site.methods)
                self.assertTrue(site.methods <= _PATTERN_METHODS, site.methods)
                # Every pattern parses into constructs the pump understands.
                list(_pumped_texts(site.pattern, site.flags))

    def test_every_unbounded_quantifier_does_linear_work(self) -> None:
        calls: dict[tuple, tuple] = {}
        for module in SWEPT_MODULES:
            for site in _regex_sites(module):
                if site.pattern in _CALL_SITE_BOUNDED:
                    call, _reason = _CALL_SITE_BOUNDED[site.pattern]
                    calls.setdefault((site.pattern, "caller"), (call, site))
                    continue
                compiled = re.compile(site.pattern, site.flags)
                for method in sorted(site.methods):
                    calls.setdefault((site.pattern, site.flags, method), (_pattern_call(compiled, method), site))

        pumps = 0
        superlinear = []
        for (pattern, *_), (call, site) in calls.items():
            for build, label in _pumped_texts(pattern, site.flags):
                pumps += 1
                small, large = build(_PUMP_SIZE), build(4 * _PUMP_SIZE)
                large_seconds = _fastest_call(call, large)
                if large_seconds < _SCREEN_FLOOR_SECONDS:
                    continue
                if large_seconds <= LINEAR_GROWTH_LIMIT * _fastest_call(call, small):
                    continue
                growth = _superlinear_growth(call, build, _PUMP_SIZE)
                if growth is not None:
                    # One reproduction per pattern is enough.
                    superlinear.append(f"{site.module}:{site.line} {pattern!r}: {label}: {growth}")
                    break
        self.assertGreater(pumps, 1000)
        self.assertEqual(superlinear, [])


if __name__ == "__main__":
    unittest.main()
