"""Malformed MPR and SES text is rejected with bounded parser work (#1216).

Python's regex engine exposes no step counter, so these tests count work
instead of timing it. Each regex call made by the parser module is charged the
square of the text region it was handed, which is the worst case for the
patterns these parsers use, and each Python line executed in the parser module
is charged one unit. Doubling a malformed input may at most roughly double that
count, and no regex may be handed a region that grows with the input.

A few whole-line patterns in the MPR event parser are linear by construction
(anchored, each quantifier followed by a character outside its class, ending in
`.+$` or `.*`, which cannot fail on a single line), and a few are literal
markers. The tests name those patterns explicitly and charge them by region
instead of region squared. Any other pattern, including a new one, is still
charged as quadratic.

The later tests measure the real regex engine as a growth ratio between two
input sizes, and sweep every pattern in the parser modules (see the section
comments below).
"""

from __future__ import annotations

import ast
import asyncio
import copy
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
from app.services import parsers_smart
from app.services import parsers_storcli
from app.services.inventory import LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, InventoryService
from app.services.parsers import (
    ParsedSSHData,
    SESMapEnclosure,
    SESMapSlot,
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
from app.services.sas_diagnostics.decoder import MAX_DIAGNOSTIC_TEXT_LENGTH
from app.services.sas_fabric_parsers import (
    parse_mpr_dmesg_events,
    parse_mpr_expanders,
    parse_mpr_sysctl_locations,
    parse_pciconf_sas_controllers,
)

# Modules whose code moved out of a measured module. `measure(module, ...)` also
# meters each module listed here for it, so calls that reach the moved code
# through a name kept on the old module are still counted.
ALSO_METERED: dict[str, tuple[types.ModuleType, ...]] = {
    parsers.__name__: (parsers_smart, parsers_storcli),
}

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
        # The controller line, its tgt/SMID/loginfo tail (read with fullmatch)
        # and the literal markers of the sense line.
        r"^(?P<controller>mpr\d+):\s+(?P<body>.+)$",
        r"tgt\s+(?P<target>[0-9]+)\s+SMID\s+(?P<smid>[0-9]+)\s+loginfo(?:\s+(?P<loginfo>.*))?",
        r"SCSI sense:",
        r"asc:",
        r"\s\(",
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
    modules = (module, *ALSO_METERED.get(module.__name__, ()))
    target_files = {metered.__file__ for metered in modules}

    def local_trace(frame, event, arg):  # noqa: ANN001, ANN202 - trace protocol
        if event == "line":
            meter.python_lines += 1
        return local_trace

    def global_trace(frame, event, arg):  # noqa: ANN001, ANN202 - trace protocol
        if frame.f_code.co_filename in target_files:
            return local_trace
        return None

    patches: list = []
    for metered in modules:
        patches.append(mock.patch.object(metered, "re", _metered_re(meter)))
        for name, value in vars(metered).items():
            if isinstance(value, re.Pattern):
                patches.append(mock.patch.object(metered, name, _MeteredPattern(value, meter)))
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
            expected = rejected(size) if callable(rejected) else rejected
            self.assertEqual(result, expected, f"{size}-character input was not rejected")
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


class MovedParserCodeIsMeteredTests(unittest.TestCase):
    """Work done by code moved out of `parsers` is still counted by `measure(parsers, ...)`."""

    # (call through a name kept on parsers, result, regex calls, regex work), as measured
    # before the SMART readers moved to parsers_smart.
    CASES = (
        ("normalize_hex_identifier", lambda: parsers.normalize_hex_identifier("0x10"), "10", 2, 32),
        (
            "_parse_sg_ses_common_field",
            lambda: parsers._parse_sg_ses_common_field("attached SAS address: 0x10"),
            ("attached_sas_address", "10"),
            2,
            32,
        ),
        (
            "parse_smartctl_text_enrichment",
            lambda: parsers.parse_smartctl_text_enrichment(
                "SATA Version is:  SATA 3.3, 6.0 Gb/s (current: 6.0 Gb/s)\n"
            )["available"],
            True,
            1,
            1444,
        ),
    )

    def test_calls_through_parsers_meter_the_moved_code(self) -> None:
        for name, call, expected, regex_calls, regex_work in self.CASES:
            with self.subTest(name=name):
                result, meter = measure(parsers, call)
                self.assertEqual(result, expected)
                self.assertEqual(meter.regex_calls, regex_calls)
                self.assertEqual(meter.regex_work, regex_work)
                self.assertGreater(meter.python_lines, 0)


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
            # The words after the bytes make the first CDB malformed; the
            # later "CDB:" is not read in its place.
            "TEST UNIT READY. CDB: 00 00 00 00 00 00 and READ(10). CDB: 28": (
                "TEST UNIT READY",
                None,
            ),
        }
        for message, (reported_operation, cdb_hex) in cases.items():
            with self.subTest(message=message):
                decoded = decode_scsi_cdb_message(message)
                self.assertEqual(decoded["reported_operation"], reported_operation)
                self.assertEqual(decoded.get("cdb_hex"), cdb_hex)
                self.assertEqual(decoded.get("cdb_malformed"), True if cdb_hex is None else None)

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

    def test_cdb_text_without_a_byte_token_is_kept_as_malformed(self) -> None:
        self.assertEqual(decode_scsi_cdb_message("WRITE(10). CDB: 2a 00")["byte_count"], 2)
        self.assertEqual(decode_scsi_cdb_message("WRITE(10). CDB:  "), {})
        for message, raw in (("WRITE(10). CDB:  \tzz", "zz"), ("WRITE(10). CDB:   ,2a", ",2a")):
            with self.subTest(message=message):
                decoded = decode_scsi_cdb_message(message)
                self.assertEqual(decoded["operation"], "WRITE(10)")
                self.assertIs(decoded["cdb_malformed"], True)
                self.assertEqual(decoded["cdb_raw"], raw)
                self.assertNotIn("cdb_hex", decoded)
        parsed = parse_mpr_dmesg_events("(da0:mpr0:0:1:0): WRITE(10). CDB:  \tzz")
        self.assertEqual(parsed["event_count"], 1)
        self.assertEqual(dict(parsed["by_controller"]["mpr0"]["operation_counts"]), {"WRITE(10)": 1})

    def test_a_later_cdb_is_not_read_in_place_of_one_outside_the_window(self) -> None:
        cdb = "28 00 00 00 00 10 00 00 01 00"
        # Control: a run shorter than the window that ends no operation lets
        # the search go on to the next "CDB:".
        control = decode_scsi_cdb_message("A" * 90 + " CDB: 00 then READ(10). CDB: " + cdb)
        self.assertEqual(control["reported_operation"], "READ(10)")
        self.assertEqual(control["cdb_hex"], cdb)
        # A run longer than the window may end an operation that starts before
        # it, so nothing is decoded, and the later "CDB:" is not read instead.
        self.assertEqual(decode_scsi_cdb_message("A" * 200 + " CDB: 00 then READ(10). CDB: " + cdb), {})
        # Separator whitespace that fills the window before a token that is not
        # a byte is refused too; one character less, the token is inside the
        # window and the CDB is kept as malformed.
        self.assertEqual(decode_scsi_cdb_message("WRITE(10). CDB:" + " " * (MAX_CDB_TEXT_CHARS - 1) + "\tzz"), {})
        inside = decode_scsi_cdb_message("WRITE(10). CDB:" + " " * (MAX_CDB_TEXT_CHARS - 2) + "\tzz")
        self.assertIs(inside["cdb_malformed"], True)
        self.assertEqual(inside["cdb_raw"], "zz")

    def test_a_wrong_width_token_at_the_window_edge_keeps_the_cdb_as_malformed(self) -> None:
        # The byte run ends one character before the window does; the next
        # token, three digits wide, crosses the edge and is not a byte.
        edge = decode_scsi_cdb_message("WRITE(10). CDB:   " + "00 " * 340 + "000 00")
        self.assertIs(edge.get("cdb_malformed"), True)
        self.assertNotIn("cdb_hex", edge)
        # A byte in its place carries the run past the window.
        self.assertEqual(decode_scsi_cdb_message("WRITE(10). CDB:   " + "00 " * 341 + "00"), {})

    def test_operation_before_an_unparsable_separator_keeps_linear_work(self) -> None:
        # The first "CDB:" has an operation but no byte after it. The decoder's
        # pattern is matched only at that operation, never searched through
        # the rest, so the search goes on at the next separator. Measured on
        # the real regex engine (see "Linear work, measured" below).
        def build(size: int) -> str:
            return "(da0:mpr0:0:1:0): READ(10). CDB:x " + "A" * size + " CDB: 00"

        self.assertEqual(decode_scsi_cdb_message(build(8000)), {})
        growth = _superlinear_growth(decode_scsi_cdb_message, build, 2000)
        self.assertIsNone(growth, growth)

    def test_the_decoder_reads_the_unbounded_pattern_inside_the_windows(self) -> None:
        reference = re.compile(
            r"(?P<operation>[A-Z][A-Z0-9 _/-]*(?:\([A-Z0-9]+\))?)\.\s+CDB:\s+(?P<cdb>\S[^\r\n]*)"
        )
        pieces = (
            "READ(10)", "WRITE(16)", "TEST UNIT READY", "x", "12", " ", "  ", "\t", ".", ". ", "CDB:", "cdb:",
            "28", "00", "2a", "zz", "000", "length", "16", "(", ")", "(10)", "\n", "\r", "A", "_", "-", "/",
        )
        rng = random.Random(1260)
        messages = [
            "(da0:mpr0:0:1:0): READ(10). CDB: 28 00 00 00 00 10 00 00 01 00 length 512 SMID 7 x",
            "x READ(10). CDB: 28 00 then WRITE(16). CDB: 8a",
            "READ(10).\tCDB:\t28\r00",
        ]
        while len(messages) < 3000:
            message = ""
            while len(message) < rng.randint(1, 90):
                message += rng.choice(pieces)
            messages.append(message)
        for message in messages:
            with self.subTest(message=message):
                expected = reference.search(message)
                found = scsi._find_cdb_message(message)
                if expected is None:
                    self.assertIsNone(found)
                else:
                    assert found is not None
                    self.assertEqual(found.group("operation", "cdb"), expected.group("operation", "cdb"))

    def test_cdb_text_reaching_the_window_end_is_not_decoded(self) -> None:
        cdb = "28 00 00 00 00 10 00 00 01 00"
        padding = MAX_CDB_TEXT_CHARS - 1 - len(cdb)
        control = decode_scsi_cdb_message("WRITE(10). CDB: " + cdb + " " * (padding - 1) + "\tlength 16")
        self.assertEqual(control["cdb_hex"], cdb)
        # The byte run plus trailing separators fills the window and the text
        # goes on: the end of the CDB is not inside the window.
        self.assertEqual(decode_scsi_cdb_message("WRITE(10). CDB: " + cdb + " " * padding + "\t00 00"), {})


def _controller_event(text: str):  # noqa: ANN202
    parsed = parse_mpr_dmesg_events(text)
    if not parsed["event_count"]:
        return None
    event = parsed["recent_events"][0]
    return event["source"], event["target"], event["smid"], event["loginfo"]


def _reported_text(value: str) -> str:
    """A text field as the event parser reports it: bounded, marked when cut."""

    if len(value) <= MAX_DIAGNOSTIC_TEXT_LENGTH:
        return value
    return f"{value[: MAX_DIAGNOSTIC_TEXT_LENGTH - 3]}..."


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
            "no asc": (lambda size: "(da0:mpr0:0:1:0): SCSI sense: X" + " " * size + "Y", (None, None, None)),
            "far asc": (
                lambda size: "(da0:mpr0:0:1:0): SCSI sense: X" + " " * size + "Y asc:11,0 (Z)",
                lambda size: (_reported_text("X" + " " * size + "Y"), "11,0", "Z"),
            ),
            # The first asc: field is read whole; there is no reason.
            "repeated asc": (
                lambda size: "(da0:mpr0:0:1:0): SCSI sense: X" + " asc:1," * (size // 7),
                lambda size: ("X", _reported_text(" asc:".join(["1,"] * (size // 7))), None),
            ),
        }
        for name, (build, expected) in shapes.items():
            with self.subTest(shape=name):
                self.assert_fabric_bounded(build, _sense_event, expected)


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
        _optional_ssh_transport_failure=InventoryService._optional_ssh_transport_failure,
        _format_optional_ssh_transport_failure=InventoryService._format_optional_ssh_transport_failure,
        _local_failure_sentence=InventoryService._local_failure_sentence,
        _ssh_failure_public_summary=InventoryService._ssh_failure_public_summary,
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


# The SES enclosure merge and the host overlay loop as they were before the
# overlay merged its devices once, kept as the reference for the tests below.
def _reference_merge_ses_enclosures(enclosures: list[SESMapEnclosure]) -> list[SESMapEnclosure]:
    for enclosure in enclosures:
        parsers._tag_control_targets_with_reported_shelf(enclosure)
    merged: dict[str, SESMapEnclosure] = {}
    identity_enclosures: dict[str, SESMapEnclosure] = {}
    slot_evidence: dict[str, list[tuple[SESMapSlot, str | None]]] = {}
    enclosure_ids: dict[str, str] = {}
    ses_device_keys: dict[tuple[str | None, str], str] = {}

    for enclosure in enclosures:
        key = enclosure_ids.get(enclosure.enclosure_id) if enclosure.enclosure_id else None
        if key is None and enclosure.ses_device:
            key = ses_device_keys.get((enclosure.ssh_host, enclosure.ses_device))
            if key is not None and (
                enclosure.enclosure_id and merged[key].enclosure_id
                and enclosure.enclosure_id != merged[key].enclosure_id
            ):
                key = None
        if key is None and enclosure.ssh_host:
            key = next((candidate_key for candidate_key, candidate in identity_enclosures.items()
                        if not (enclosure.enclosure_id and merged[candidate_key].enclosure_id
                                and enclosure.enclosure_id != merged[candidate_key].enclosure_id)
                        if parsers._ses_enclosures_share_identity(candidate, enclosure)), None)
        if key is None:
            key = enclosure.enclosure_id or enclosure.ses_device or enclosure.enclosure_name or f"unknown-{len(merged)}"
            if key in merged:
                key = f"{key}@{enclosure.ssh_host or 'local'}:{len(merged)}"
            ses_devices = list(enclosure.ses_devices)
            if enclosure.ses_device and enclosure.ses_device not in ses_devices:
                ses_devices.append(enclosure.ses_device)
            merged[key] = SESMapEnclosure(
                ses_device=enclosure.ses_device,
                ssh_host=enclosure.ssh_host,
                ses_devices=ses_devices,
                enclosure_id=enclosure.enclosure_id,
                enclosure_name=enclosure.enclosure_name,
                enclosure_label=enclosure.enclosure_label,
                profile_id=enclosure.profile_id,
                layout_rows=enclosure.layout_rows,
                layout_columns=enclosure.layout_columns,
                slot_layout=enclosure.slot_layout,
                slots={},
            )
            slot_evidence[key] = []
            identity_enclosures[key] = enclosure
        target = merged[key]
        target.enclosure_id = target.enclosure_id or enclosure.enclosure_id
        target.enclosure_name = target.enclosure_name or enclosure.enclosure_name
        target.ses_device = target.ses_device or enclosure.ses_device
        for ses_device in [*enclosure.ses_devices, enclosure.ses_device]:
            if ses_device and ses_device not in target.ses_devices:
                target.ses_devices.append(ses_device)
        for ses_device, count in enclosure.unplaced_sysfs_bindings_by_ses_device.items():
            target.unplaced_sysfs_bindings_by_ses_device[ses_device] = (
                target.unplaced_sysfs_bindings_by_ses_device.get(ses_device, 0) + count
            )
        target.enclosure_label = target.enclosure_label or enclosure.enclosure_label
        target.profile_id = target.profile_id or enclosure.profile_id
        target.layout_rows = target.layout_rows or enclosure.layout_rows
        target.layout_columns = target.layout_columns or enclosure.layout_columns
        target.slot_layout = target.slot_layout or enclosure.slot_layout
        if target.enclosure_id:
            enclosure_ids.setdefault(target.enclosure_id, key)
        if enclosure.ses_device:
            ses_device_keys.setdefault((enclosure.ssh_host, enclosure.ses_device), key)
        slot_evidence[key].extend((slot, enclosure.ssh_host) for slot in enclosure.slots.values())

        for slot in enclosure.unmapped_slots:
            parsers._record_unmapped_ses_slot(target, slot)

    for key, target in merged.items():
        slots_by_element: list[SESMapSlot] = []
        element_positions: dict[tuple[str | None, str, int], int] = {}
        for slot, ssh_host in slot_evidence[key]:
            ses_device = slot.ses_device or target.ses_device or ""
            identity = (
                (ssh_host, ses_device, slot.element_id)
                if slot.element_id is not None
                else None
            )
            if identity is None or identity not in element_positions:
                if identity is not None:
                    element_positions[identity] = len(slots_by_element)
                slots_by_element.append(slot)
                continue
            parsers._merge_ses_slot_evidence(slots_by_element[element_positions[identity]], slot)

        for slot in slots_by_element:
            existing = target.slots.get(slot.slot_number)
            if existing is None:
                target.slots[slot.slot_number] = slot
                continue
            parsers._merge_ses_slot_evidence(existing, slot)

    return list(merged.values())


def _reference_host_overlay(slot_count: int, host: str, devices: list[str], results: list[SSHCommandResult]):  # noqa: ANN202
    """The previous loop: each device's result merged into the overlay so far."""

    by_command = {result.command: result for result in results}
    sysfs = by_command.get(LINUX_ENCLOSURE_SYSFS_MAP_COMMAND)
    with mock.patch.object(parsers, "_merge_ses_enclosures", _reference_merge_ses_enclosures), mock.patch(
        "app.services.inventory._merge_ses_enclosures", _reference_merge_ses_enclosures
    ):
        sysfs_slots = parsers.parse_enclosure_sysfs_map(sysfs.stdout) if sysfs and sysfs.ok and sysfs.stdout else {}
        overlay, failures = ParsedSSHData(), []
        for device in devices:
            aes, ec, join = (
                f"sudo -n /usr/bin/sg_ses -p aes {device}",
                f"sudo -n /usr/bin/sg_ses -p ec {device}",
                f"sudo -n /usr/bin/sg_ses --join --filter {device}",
            )
            aes_result = by_command.get(aes)
            if aes_result is None or not aes_result.ok:
                failures.append(device)
                continue
            outputs = {aes: aes_result.stdout}
            outputs.update({command: by_command[command].stdout for command in (ec, join)
                            if command in by_command and by_command[command].ok})
            parsed = parse_ssh_outputs(outputs, slot_count, None, None, enclosure_sysfs_slots=sysfs_slots)
            if not parsed.ses_enclosures:
                continue
            InventoryService._tag_ses_overlay(parsed, host)
            overlay = InventoryService._merge_ses_overlay_data(overlay, parsed)
    return overlay, failures


_ENCLOSURE_ID_LINE = re.compile(r"(logical identifier \(hex\): )([0-9a-fA-F]+)")
# Checked-in page sets: (file prefix, pages, sg name in the sysfs map, sysfs map).
_SES_FIXTURE_SETS = (
    ("scale_offset_1based", ("aes", "ec", "join"), "sg9", "scale_offset_1based_sysfs.txt"),
    ("scale_offset_1based_invalid", ("aes",), "sg9", "scale_offset_1based_sysfs.txt"),
    ("scale_md1280_sg1", ("aes", "ec", "join"), "sg1", "scale_md1280_sysfs.txt"),
    ("scale_md1280_sg76", ("aes", "ec", "join"), "sg76", "scale_md1280_sysfs.txt"),
    ("scale_sg26", ("aes", "ec"), "sg26", None),
    ("scale_sg37", ("aes", "ec"), "sg37", None),
    ("scale_shared_sata", ("aes",), "sg0", "scale_shared_sata_sysfs.txt"),
)


def _fixture_text(name: str) -> str:
    return (_PARITY_FIXTURES / name).read_text(encoding="utf-8")


def _generated_ses_host(rng: random.Random, size: int):  # noqa: ANN202
    """SES devices on one host built from the checked-in page sets.

    Each device reads one page set. Its enclosure identifier is the checked-in
    one (the same set on two devices is one enclosure seen on two paths), one
    shared by every device reading that set, one of its own, or absent. Some
    devices return no AES page or an empty one.
    """

    devices, results, sysfs_lines = [], [], []
    for index in range(size):
        device = f"/dev/sg{100 + index}"
        devices.append(device)
        prefix, pages, sg_name, sysfs_name = rng.choice(_SES_FIXTURE_SETS)
        identifier = rng.choice(("checked-in", "shared", "own", "absent"))
        outcome = rng.choices(("pages", "aes failed", "aes empty"), weights=(8, 1, 1))[0]
        for page in pages:
            if page != "aes" and rng.random() < 0.3:
                continue
            text = _fixture_text(f"{prefix}_{page}.txt")
            if identifier == "shared":
                text = _ENCLOSURE_ID_LINE.sub(lambda match: match.group(1) + f"6{len(prefix):015x}", text)
            elif identifier == "own":
                text = _ENCLOSURE_ID_LINE.sub(lambda match: match.group(1) + f"7{index:015x}", text)
            elif identifier == "absent":
                text = _ENCLOSURE_ID_LINE.sub("", text)
            ok = not (page == "aes" and outcome == "aes failed")
            if page == "aes" and outcome == "aes empty":
                text = ""
            command = (
                f"sudo -n /usr/bin/sg_ses --join --filter {device}"
                if page == "join"
                else f"sudo -n /usr/bin/sg_ses -p {page} {device}"
            )
            results.append(SSHCommandResult(
                command=command, ok=ok, stdout=text if ok else "", stderr="" if ok else "failed", exit_code=0 if ok else 1
            ))
        if sysfs_name and rng.random() < 0.8:
            for line in _fixture_text(sysfs_name).splitlines():
                sysfs_lines.append(line.replace(f"|{sg_name} |", f"|sg{100 + index} |"))
    results.append(SSHCommandResult(
        command=LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, ok=True, stdout="".join(f"{line}\n" for line in sysfs_lines),
        stderr="", exit_code=0,
    ))
    rng.shuffle(results)
    return devices, results


def _generated_enclosures(rng: random.Random) -> list[SESMapEnclosure]:
    """Parsed-looking enclosures: shared, distinct and missing identifiers,
    devices seen on two paths, and hosts with and without bay evidence."""

    hosts = rng.choice((("host-a",), ("host-a", "host-b"), ("host-a", "host-b", "host-c"), ("host-a", None), (None,)))
    identifiers = rng.choice(((None,), (None, "E1"), ("E1",), ("E1", "E2", None), tuple(f"E{i}" for i in range(5))))
    devices = [f"/dev/sg{index}" for index in range(rng.randrange(1, 5))]
    enclosures = []
    for _ in range(rng.randrange(0, 9)):
        device = rng.choice((*devices, None))
        enclosure = SESMapEnclosure(
            ses_device=device,
            ses_devices=rng.choice(([], [device] if device else [], [rng.choice(devices)])),
            enclosure_id=rng.choice(identifiers),
            enclosure_name=rng.choice((None, "Front", "Rear")),
            profile_id=rng.choice((None, "profile")),
            layout_rows=rng.choice((None, 2)),
            unplaced_sysfs_bindings_by_ses_device=rng.choice(({}, {device or "": 1})),
            ssh_host=rng.choice(hosts),
        )
        for _ in range(rng.randrange(4)):
            slot = SESMapSlot(
                slot_number=rng.randrange(2),
                element_id=rng.choice((None, 0, 1, 2, 3)),
                ses_device=rng.choice((None, device)),
                slot_number_source=rng.choice((None, "ses_element_id_fallback", "ses_device_slot_number")),
                present=rng.choice((None, True, False)),
                presence_source=rng.choice((None, "aes", "ec")),
                device_names=rng.choice(([], ["sda"], ["sdb"])),
                device_names_source=rng.choice((None, "enclosure_sysfs")),
                sas_address=rng.choice((None, "0x10", "0x11", "0x11", "0x0")),
                attached_sas_address=rng.choice((None, "0x20", "0x20", "0x21")),
                serial=rng.choice((None, "S1", "S2")),
                control_targets=[{"ses_device": device, "ses_element_id": rng.randrange(3)}] if device else [],
            )
            enclosure.slots.setdefault(slot.slot_number, slot)
        for _ in range(rng.randrange(3)):
            enclosure.unmapped_slots.append(
                SESMapSlot(slot_number=-1, element_id=rng.randrange(6), ses_device=rng.choice((None, device)))
            )
        enclosures.append(enclosure)
    return enclosures


def _distinct_enclosures(size: int, *, one_device: bool = False) -> list[SESMapEnclosure]:
    return [
        SESMapEnclosure(
            ses_device="/dev/sg0" if one_device else f"/dev/sg{index}",
            enclosure_id=f"5{index:015x}",
            ssh_host="host-a",
        )
        for index in range(size)
    ]


def _synthetic_shelf_pages(
    bays: int, *, identifier: str | None, elements: list[int] | None = None, empty: tuple[int, ...] = (),
    invalid: tuple[int, ...] = (), pages: tuple[str, ...] = ("aes", "ec", "join"),
) -> dict[str, str]:
    """One SES path to a shelf: bay b (device slot number b + 1) is element
    elements[b]. An empty bay has no SAS device; an invalid one has the normal
    INVALID additional-element descriptor of an empty bay."""

    elements = list(range(bays)) if elements is None else elements
    title = "  SYNTH SHELF 0100\n" + (f"  Primary enclosure logical identifier (hex): {identifier}\n" if identifier else "")
    out = {page: title for page in pages}
    headers = {
        "aes": "Additional element status diagnostic page:\n Element type: Array device slot, subenclosure id: 0 [ti=0]\n",
        "ec": "Enclosure Status diagnostic page:\n Element type: Array device slot, subenclosure id: 0 [ti=0]\n",
        "join": "[0,-1] Element type: Array device slot\n",
    }
    for page in pages:
        out[page] += headers[page]
    for bay, element in sorted(enumerate(elements), key=lambda item: item[1]):
        present = bay not in empty
        fields = (
            f" number of phys: 1, not all phys: 0, device slot number: {bay + 1}\n SAS device type: "
            + ("end device" if present else "no SAS device attached")
            + f"\n attached SAS address: 0x5ddd\n SAS address: {f'0x{0x500000 + bay:x}' if present else '0x0'}\n"
        )
        aes = " flagged as invalid (no further information)\n" if bay in invalid else fields
        status = "OK" if present else "Not installed"
        if "aes" in out:
            out["aes"] += f" Element index: {element} eiioe=0\n" + aes
        if "ec" in out:
            out["ec"] += f" Element {element} descriptor:\n status: {status}\n Ident=0\n"
        if "join" in out:
            out["join"] += (
                f"Slot{bay + 1:02} [0,{element}] Element type: Array device slot\n Enclosure Status:\n"
                f" status: {status}\n Additional Element Status:\n" + aes
            )
    return out


def _reversed_element_pages(pages: dict[str, str]) -> dict[str, str]:
    """The checked-in four-bay pages with the elements numbered the other way
    round, each page's descriptors in element order as the tool prints them."""

    patterns = {
        "aes": r"(?m)^      Element index: (\d+).*$",
        "ec": r"(?m)^      Element (\d+) descriptor:.*$",
        "join": r"(?m)^Slot\d+ \[0,(\d+)\].*$",
    }
    out = {}
    for page, text in pages.items():
        matches = list(re.finditer(patterns[page], text))
        blocks = []
        for position, match in enumerate(matches):
            block = text[match.start():matches[position + 1].start() if position + 1 < len(matches) else len(text)]
            element = 3 - int(match.group(1))
            start, end = match.start(1) - match.start(), match.end(1) - match.start()
            blocks.append((element, block[:start] + str(element) + block[end:]))
        out[page] = text[:matches[0].start()] + "".join(block for _, block in sorted(blocks))
    return out


def _host_results(device_pages: list[dict[str, str]]):  # noqa: ANN202
    devices = [f"/dev/sg{100 + index}" for index in range(len(device_pages))]
    results = [result for device, pages in zip(devices, device_pages) for result in _overlay_results([device], pages, "")]
    results = [result for result in results if result.command != LINUX_ENCLOSURE_SYSFS_MAP_COMMAND]
    results.append(SSHCommandResult(command=LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, ok=True, stdout="", stderr="",
                                    exit_code=0))
    return devices, results


def _empty_bay_redundant_paths(*, later_shelf: bool, path_b_first: bool = False) -> list[dict[str, str]]:
    """The checked-in four-bay shelf on two paths. Path A reads empty bay 3 as
    the normal INVALID descriptor; path B numbers the elements the other way
    round. A later device may read a second shelf."""

    pages = {page: _fixture_text(f"scale_offset_1based_{page}.txt") for page in ("aes", "ec", "join")}
    path_a = dict(
        pages,
        aes=_fixture_text("scale_offset_1based_invalid_aes.txt"),
        join=re.sub(
            r"(?ms)(^Slot03 .*?  Additional Element Status:\n).*?(?=^Slot04)",
            lambda match: match.group(1) + "    flagged as invalid (no further information)\n",
            pages["join"],
        ),
    )
    paths = [_reversed_element_pages(pages), path_a] if path_b_first else [path_a, _reversed_element_pages(pages)]
    if later_shelf:
        paths.append({page: _ENCLOSURE_ID_LINE.sub(lambda match: match.group(1) + "5bbbb", text)
                      for page, text in pages.items()})
    return paths


def _two_host_service(host_pages: dict[str, list[dict[str, str]]]) -> types.SimpleNamespace:
    service = types.SimpleNamespace(
        settings=types.SimpleNamespace(layout=types.SimpleNamespace(slot_count=4)),
        _optional_ssh_transport_failure=InventoryService._optional_ssh_transport_failure,
        _format_optional_ssh_transport_failure=InventoryService._format_optional_ssh_transport_failure,
        _local_failure_sentence=InventoryService._local_failure_sentence,
        _ssh_failure_public_summary=InventoryService._ssh_failure_public_summary,
        _tag_ses_overlay=InventoryService._tag_ses_overlay,
        _merge_ses_overlay_data=InventoryService._merge_ses_overlay_data,
        _score_sg_ses_overlay=InventoryService._score_sg_ses_overlay,
        _get_cached_sg_ses_devices=lambda host: [],
        _remember_sg_ses_devices=lambda host, devices: None,
        _sg_ses_device_cache={},
        scores={},
    )

    async def discover(host, *, failure_prefix, report_empty_discovery=True):  # noqa: ANN001, ANN202
        devices, results = _host_results(host_pages[host])
        overlay, failures = await InventoryService._fetch_sg_ses_host_overlay(
            service, host, devices, failure_prefix=failure_prefix, command_results=results
        )
        service.scores[host] = InventoryService._score_sg_ses_overlay(overlay)
        return devices, overlay, failures

    async def discover_read(host, *, failure_prefix, report_empty_discovery=True):  # noqa: ANN001, ANN202
        # The whole device list was read: no incomplete-read warning.
        return (*await discover(host, failure_prefix=failure_prefix, report_empty_discovery=report_empty_discovery), None)

    service._discover_and_fetch_sg_ses_host_overlay = discover
    service._discover_sg_ses_host_read = discover_read
    return service


class SesOverlayMergeWorkTests(LinearGrowthAssertions):
    def test_enclosure_merge_matches_the_previous_merge(self) -> None:
        rng = random.Random(1216)
        for case in range(3000):
            enclosures = _generated_enclosures(rng)
            expected = _reference_merge_ses_enclosures(copy.deepcopy(enclosures))
            self.assertEqual(parsers._merge_ses_enclosures(copy.deepcopy(enclosures)), expected, case)

    def test_a_record_joins_the_earliest_match_on_another_host(self) -> None:
        def enclosure(host, device, *bays, enclosure_id=None):  # noqa: ANN001, ANN202
            slots = {
                slot: SESMapSlot(slot_number=slot, sas_address=address, attached_sas_address="0x20")
                for slot, address in bays
            }
            return SESMapEnclosure(ses_device=device, enclosure_id=enclosure_id, ssh_host=host, slots=slots)

        cases = {
            # host-b is indexed first, but its match comes after host-c's.
            "earliest across hosts": [
                enclosure("host-b", "/dev/sg1", (0, "0x30")),
                enclosure("host-c", "/dev/sg2", (0, "0x10")),
                enclosure("host-b", "/dev/sg3", (1, "0x11")),
                enclosure("host-a", "/dev/sg9", (0, "0x10"), (1, "0x11")),
            ],
            # The first match took another ID after it was indexed.
            "identifier taken later": [
                enclosure("host-b", "/dev/sg1", (0, "0x10")),
                enclosure("host-b", "/dev/sg1", enclosure_id="E1"),
                enclosure("host-c", "/dev/sg2", (1, "0x11")),
                enclosure("host-a", "/dev/sg9", (0, "0x10"), (1, "0x11"), enclosure_id="E9"),
            ],
        }
        for name, enclosures in cases.items():
            with self.subTest(name):
                merged = parsers._merge_ses_enclosures(copy.deepcopy(enclosures))
                self.assertEqual(merged, _reference_merge_ses_enclosures(copy.deepcopy(enclosures)))
                self.assertEqual(len(merged), 3 if name == "earliest across hosts" else 2)
                self.assertEqual(next(item.ssh_host for item in merged if "/dev/sg9" in item.ses_devices), "host-c")

    def test_slot_candidates_merged_in_place_match_merging_one_by_one(self) -> None:
        rng = random.Random(926)

        def candidate_map():  # noqa: ANN202
            return {
                rng.randrange(4): {
                    "present": rng.choice((None, True, False)),
                    "presence_source": rng.choice((None, "aes", "ec")),
                    "device_names": rng.choice(([], ["sda"], ["sdb"])),
                    "device_names_source": rng.choice((None, "enclosure_sysfs")),
                    "identify_active": rng.choice((None, True, False)),
                    "ses_targets": [
                        {
                            "ssh_host": "host-a",
                            "ses_device": rng.choice(("/dev/sg0", "/dev/sg1")),
                            "ses_element_id": rng.randrange(3),
                            "ses_slot_number": rng.choice((None, 1)),
                            **rng.choice(({}, {"enclosure_id": "E1"}, {"enclosure_id": "E2"})),
                        }
                        for _ in range(rng.randrange(4))
                    ],
                }
                for _ in range(rng.randrange(4))
            }

        for case in range(1000):
            maps = [candidate_map() for _ in range(rng.randrange(1, 7))]
            expected: dict = {}
            for item in copy.deepcopy(maps):
                expected = parsers.merge_slot_candidate_maps(expected, item)
            merged: dict = {}
            index: dict = {}
            for item in copy.deepcopy(maps):
                parsers.merge_slot_candidate_maps(merged, item, index)
            self.assertEqual(merged, expected, case)

    def test_host_overlay_matches_the_previous_overlay(self) -> None:
        rng = random.Random(1260)
        for case in range(120):
            devices, results = _generated_ses_host(rng, rng.randrange(1, 6))
            slot_count = rng.choice((4, 60, 84))
            expected, expected_failures = _reference_host_overlay(slot_count, "host-a", devices, copy.deepcopy(results))
            overlay, failures = _fetch_overlay(_overlay_stub(slot_count), devices, copy.deepcopy(results))
            self.assertEqual(overlay.ses_enclosures, expected.ses_enclosures, case)
            self.assertEqual(overlay.ses_slot_candidates, expected.ses_slot_candidates, case)
            self.assertEqual(overlay.ses_selected_meta, expected.ses_selected_meta, case)
            self.assertEqual(len(failures), len(expected_failures), case)

    def test_checked_in_pages_give_the_previous_overlay(self) -> None:
        devices, results = [], []
        sysfs = []
        for index, (prefix, pages, sg_name, sysfs_name) in enumerate(_SES_FIXTURE_SETS * 2):
            device = f"/dev/sg{200 + index}"
            devices.append(device)
            results.extend(_overlay_results([device], {page: _fixture_text(f"{prefix}_{page}.txt") for page in pages}, "")[:-1])
            if sysfs_name:
                sysfs.extend(line.replace(f"|{sg_name} |", f"|sg{200 + index} |")
                             for line in _fixture_text(sysfs_name).splitlines())
        results.append(SSHCommandResult(
            command=LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, ok=True, stdout="\n".join(sysfs) + "\n", stderr="", exit_code=0
        ))
        expected, _ = _reference_host_overlay(84, "host-a", devices, copy.deepcopy(results))
        overlay, failures = _fetch_overlay(_overlay_stub(84), devices, results)
        self.assertEqual(failures, [])
        # Each checked-in set is one enclosure read on two paths.
        self.assertEqual(len(overlay.ses_enclosures), 6)
        self.assertEqual(overlay.ses_enclosures, expected.ses_enclosures)
        self.assertEqual(overlay.ses_slot_candidates, expected.ses_slot_candidates)
        self.assertEqual(overlay.ses_selected_meta, expected.ses_selected_meta)

    def test_many_ses_devices_have_linear_merge_work(self) -> None:
        aes = _fixture_text("scale_offset_1based_aes.txt")
        sysfs_line = _fixture_text("scale_offset_1based_sysfs.txt").splitlines()[0]

        def build(size: int):  # noqa: ANN202
            devices = [f"/dev/sg{index}" for index in range(size)]
            pages = {
                device: _ENCLOSURE_ID_LINE.sub(lambda match: match.group(1) + f"7{index:015x}", aes)
                for index, device in enumerate(devices)
            }
            results = [
                SSHCommandResult(command=f"sudo -n /usr/bin/sg_ses -p aes {device}", ok=True, stdout=text,
                                 stderr="", exit_code=0)
                for device, text in pages.items()
            ]
            sysfs = "".join(sysfs_line.replace("|sg9 |", f"|sg{index} |") + "\n" for index in range(size))
            results.append(SSHCommandResult(command=LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, ok=True, stdout=sysfs,
                                            stderr="", exit_code=0))
            return devices, results

        stub = _overlay_stub(4)
        devices, results = build(8)
        overlay, failures = _fetch_overlay(stub, devices, results)
        self.assertEqual(failures, [])
        self.assertEqual([enclosure.ses_device for enclosure in overlay.ses_enclosures], devices)
        self.assert_linear_growth(lambda built: _fetch_overlay(stub, *built), build, size=256)

    def test_many_distinct_enclosures_have_linear_merge_work(self) -> None:
        for one_device in (False, True):
            with self.subTest(one_device=one_device):
                merged = parsers._merge_ses_enclosures(_distinct_enclosures(5, one_device=one_device))
                self.assertEqual(len(merged), 5)
                self.assert_linear_growth(
                    parsers._merge_ses_enclosures,
                    lambda size: _distinct_enclosures(size, one_device=one_device),
                    size=1500,
                )

    def test_slot_candidates_keep_the_same_element_seen_from_two_hosts(self) -> None:
        # Two HA nodes reach the same element through a device with the same
        # name. Each node needs its own target, in the order they were merged.
        def target(host: str, element: int) -> dict:
            return {"ssh_host": host, "ses_device": "/dev/sg0", "ses_element_id": element, "ses_slot_number": 1}

        maps = [{0: {"ses_targets": [target("host-a", 1)]}}, {0: {"ses_targets": [target("host-b", 1), target("host-a", 1)]}}]
        expected = [target("host-a", 1), target("host-b", 1)]
        merged: dict = {}
        index: dict = {}
        for item in copy.deepcopy(maps):
            parsers.merge_slot_candidate_maps(merged, item, index)
        self.assertEqual(merged[0]["ses_targets"], expected)
        self.assertEqual(sorted(key[0] for key in index[0]), ["host-a", "host-b"])
        one_by_one: dict = {}
        for item in copy.deepcopy(maps):
            one_by_one = parsers.merge_slot_candidate_maps(one_by_one, item)
        self.assertEqual(one_by_one[0]["ses_targets"], expected)

    def assert_previous_overlay(self, device_pages: list[dict[str, str]], slot_count: int, case: object = None):  # noqa: ANN201
        devices, results = _host_results(device_pages)
        expected, expected_failures = _reference_host_overlay(slot_count, "host-a", devices, copy.deepcopy(results))
        overlay, failures = _fetch_overlay(_overlay_stub(slot_count), devices, results)
        self.assertEqual(failures, expected_failures, case)
        self.assertEqual(overlay.ses_enclosures, expected.ses_enclosures, case)
        self.assertEqual([list(item.slots) for item in overlay.ses_enclosures],
                         [list(item.slots) for item in expected.ses_enclosures], case)
        self.assertEqual(overlay.ses_slot_candidates, expected.ses_slot_candidates, case)
        self.assertEqual(overlay.ses_selected_meta, expected.ses_selected_meta, case)
        return overlay

    def test_redundant_paths_with_an_empty_bay_keep_the_previous_answer(self) -> None:
        # The answer pinned here is inherited and kept on purpose: this change
        # bounds the work, not the answer. Path B's stronger evidence for empty
        # bay 3 brings its element number to a record that keeps path A's
        # device, which names path A's bay 2. The earlier loop merged bay 3 into
        # bay 2 when it merged the next device, so a later shelf decides
        # whether bay 3 is shown. Tracked in the follow-up "SES merge: a bay
        # keeps its own enclosure device and element together on every path".
        for later_shelf, bays, score in ((True, [1, 2, 4], 7), (False, [1, 2, 4, 3], 4)):
            with self.subTest(later_shelf=later_shelf):
                overlay = self.assert_previous_overlay(_empty_bay_redundant_paths(later_shelf=later_shelf), 4)
                shelf = overlay.ses_enclosures[0]
                self.assertEqual(list(shelf.slots), bays)
                self.assertEqual(InventoryService._score_sg_ses_overlay(overlay), score)
                targets = [(item["ses_device"], item["ses_element_id"]) for item in shelf.slots[2].control_targets]
                if later_shelf:
                    # Bay 2 also carries bay 3's two targets, as before.
                    self.assertEqual(targets, [("/dev/sg100", 1), ("/dev/sg101", 2), ("/dev/sg101", 1), ("/dev/sg100", 2)])
                else:
                    self.assertEqual(targets, [("/dev/sg100", 1), ("/dev/sg101", 2)])

    def test_two_hosts_choose_the_host_the_previous_overlay_chose(self) -> None:
        # Two hosts read the same shelves; one enumerates the paths the other
        # way round, so only that host's overlay keeps bay 3. The host with more
        # bays is chosen, as it was before.
        for first_host_path_b_first in (False, True):
            with self.subTest(first_host_path_b_first=first_host_path_b_first):
                host_pages = {
                    "node-a": _empty_bay_redundant_paths(later_shelf=True, path_b_first=first_host_path_b_first),
                    "node-b": _empty_bay_redundant_paths(later_shelf=True, path_b_first=not first_host_path_b_first),
                }
                service = _two_host_service(host_pages)
                overlay, failures, best_host = asyncio.run(
                    InventoryService._fetch_sg_ses_overlay(service, ["node-a", "node-b"], failure_prefix="SES")
                )
                chosen = "node-a" if first_host_path_b_first else "node-b"
                self.assertEqual(failures, [])
                self.assertEqual(service.scores, {"node-a": 8 if first_host_path_b_first else 7,
                                                  "node-b": 7 if first_host_path_b_first else 8})
                self.assertEqual(best_host, chosen)
                devices, results = _host_results(host_pages[chosen])
                expected, _ = _reference_host_overlay(4, chosen, devices, results)
                self.assertEqual(overlay.ses_enclosures, expected.ses_enclosures)
                self.assertEqual(sorted(overlay.ses_enclosures[0].slots), [1, 2, 3, 4])

    def test_redundant_paths_with_independent_numbering_give_the_previous_overlay(self) -> None:
        # Shelves on two to four paths, each path numbering the elements its own
        # way, an empty bay with the INVALID descriptor, other shelves between
        # and after them, devices in any order.
        rng = random.Random(7260)
        for case in range(60):
            bays = rng.choice((4, 12))
            device_pages = []
            for path in range(rng.choice((2, 2, 3, 4))):
                elements = list(range(bays))
                if path:
                    rng.shuffle(elements)
                device_pages.append(_synthetic_shelf_pages(
                    bays, identifier="5abc", elements=elements, empty=(2,), invalid=(2,),
                    pages=rng.choice((("aes",), ("aes", "ec"), ("aes", "ec", "join"))),
                ))
            for other in range(rng.randrange(4)):
                elements = list(range(4))
                rng.shuffle(elements)
                device_pages.append(_synthetic_shelf_pages(
                    4, identifier=f"5d{other:02x}", elements=elements, empty=(1,), invalid=(1,) if rng.random() < 0.5 else ()
                ))
            rng.shuffle(device_pages)
            self.assert_previous_overlay(device_pages, bays, case)

    def test_redundant_paths_with_an_empty_bay_have_linear_merge_work(self) -> None:
        # Every shelf is re-merged after its second path, as the earlier loop
        # did, but the shelves merged before it are not merged again.
        def build(size: int) -> list[dict[str, str]]:
            device_pages = []
            for index in range(size):
                identifier = f"5{index:04x}"
                device_pages.append(_synthetic_shelf_pages(4, identifier=identifier, empty=(2,), invalid=(2,)))
                device_pages.append(_synthetic_shelf_pages(4, identifier=identifier, elements=[3, 2, 1, 0], empty=(2,)))
            return device_pages

        real = parsers._merge_ses_enclosures

        def merged_slots(device_pages: list[dict[str, str]]) -> int:
            handed = []

            def counting(enclosures):  # noqa: ANN001, ANN202
                handed.append(sum(len(item.slots) for item in enclosures))
                return real(enclosures)

            with mock.patch("app.services.inventory._merge_ses_enclosures", counting):
                overlay, failures = _fetch_overlay(_overlay_stub(4), *_host_results(device_pages))
            self.assertEqual(failures, [])
            self.assertEqual(len(overlay.ses_enclosures), len(device_pages) // 2)
            return sum(handed)

        # Bays handed to the merge, per shelf: its two paths, then the shelf
        # again once or twice until a merge leaves it unchanged. Merging every
        # earlier shelf again for each device would grow with the square.
        for size in (16, 64):
            self.assertLessEqual(merged_slots(build(size)), size * 4 * 5, size)
        stub = _overlay_stub(4)
        self.assert_linear_growth(lambda built: _fetch_overlay(stub, *built), lambda size: _host_results(build(size)),
                                  size=64)


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

SWEPT_MODULES = (parsers, parsers_smart, parsers_storcli, sas_fabric_parsers, scsi)
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
    r"(?P<operation>[A-Z][A-Z0-9 _/-]*(?:\([A-Z0-9]+\))?)\.\s+CDB:\s+(?P<cdb>\S[^\r\n]*)": (
        decode_scsi_cdb_message,
        "matched only at an operation found in the window before CDB:",
    ),
    r"[A-Z][A-Z0-9 _/-]*(?:\([A-Z0-9]+\))?\.\s+\Z": (
        decode_scsi_cdb_message,
        "searched only in the window before CDB:",
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
    # A pattern compiled from each string of a literal tuple in a comprehension.
    looped: dict[int, tuple[str | None, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)) and len(node.generators) == 1:
            loop = node.generators[0]
            if isinstance(loop.target, ast.Name) and isinstance(loop.iter, ast.Tuple):
                for child in ast.walk(node.elt):
                    first = child.args[0] if _re_call(child) and child.args else None
                    if isinstance(first, ast.Name) and first.id == loop.target.id:
                        looped[id(child)] = tuple(_literal_pattern(item) for item in loop.iter.elts)

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
        literals = (pattern,) if pattern is not None else looped.get(id(node), (None,))
        if None in literals:
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
        sites.extend(_RegexSite(name, node.lineno, literal, flags, methods) for literal in literals)
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
