"""The StorCLI readers live in parsers_storcli; parsers keeps every name.

`app.services.parsers_storcli` holds the StorCLI JSON readers and
`_coerce_int_like`, moved out of `app.services.parsers` without a change.
These tests pin what the move must keep: the new module imports on its own,
every moved name is still the same object on the old module, a replacement
made on the old module still reaches the code that stayed there, and the
parser work meters and the regex sweep still see the moved code.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every top-level name defined in app.services.parsers_storcli, in source order.
MOVED_NAMES = (
    "_coerce_int_like",
    "_storcli_controller_id",
    "_parse_storcli_controllers",
    "_parse_storcli_json",
    "parse_storcli_controller_info",
    "_storcli_slot_key",
    "_parse_storcli_eid_slot",
    "_extract_storcli_drive_path",
    "_flatten_storcli_detail",
    "_first_detail_value",
    "_collect_storcli_drive_details",
    "_collect_storcli_physical_drive_rows",
    "_parse_storcli_physical_drive_response",
    "parse_storcli_physical_drives",
    "storcli_physical_drive_list_complete",
    "_collect_storcli_virtual_drive_details",
    "_collect_storcli_virtual_drive_rows",
    "_STORCLI_PD_SECTION_HEADING_PATTERN",
    "_STORCLI_PD_SECTION_VD_PATTERN",
    "_collect_storcli_pd_sections",
    "_parse_storcli_virtual_drive_response",
    "parse_storcli_virtual_drives",
)
# The modules and names parsers_storcli imports; every other name in it is moved.
STORCLI_IMPORTS = ("annotations", "json", "re", "Any", "normalize_text", "normalize_value_text")


def _response(response_data: dict, status: str = "Success") -> str:
    return json.dumps({"Controllers": [{"Command Status": {"Status": status}, "Response Data": response_data}]})


class StorcliModuleSplitTests(unittest.TestCase):
    def test_new_module_imports_without_the_old_module(self) -> None:
        code = (
            "import sys\n"
            "import app.services.parsers_storcli as storcli\n"
            "assert 'app.services.parsers' not in sys.modules, 'parsers_storcli imported parsers'\n"
            "print(storcli.parse_storcli_controller_info('{}'), storcli._coerce_int_like('12 C'))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "{} 12")

    def test_old_module_keeps_every_moved_name_as_the_same_object(self) -> None:
        import app.services.parsers as parsers
        import app.services.parsers_storcli as storcli

        self.assertEqual(len(MOVED_NAMES), 22)
        storcli_names = {name for name in vars(storcli) if not name.startswith("__")}
        self.assertEqual(storcli_names, set(MOVED_NAMES) | set(STORCLI_IMPORTS))
        for name in MOVED_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(parsers, name), getattr(storcli, name))
        for name in STORCLI_IMPORTS[1:]:
            with self.subTest(imported=name):
                self.assertIs(getattr(storcli, name), getattr(parsers, name))
        # The compiled patterns are the module's own objects, not copies.
        self.assertEqual(parsers._STORCLI_PD_SECTION_HEADING_PATTERN.pattern, r"\bPDs\s+for\s+VD\s+")
        self.assertEqual(parsers._STORCLI_PD_SECTION_HEADING_PATTERN.flags & re.IGNORECASE, re.IGNORECASE)
        self.assertEqual(parsers._STORCLI_PD_SECTION_VD_PATTERN.pattern, r"\w*")

    def test_replacing_a_moved_name_on_the_old_module_reaches_the_code_that_stayed(self) -> None:
        import app.services.parsers as parsers
        import app.services.parsers_storcli as storcli

        # parse_esxcli_smart_get stayed in parsers and reads _coerce_int_like there.
        self.assertIs(parsers._coerce_int_like, storcli._coerce_int_like)
        smart_output = "Parameter  Value  Threshold  Worst\nDrive Temperature  35  N/A  N/A\n"
        self.assertEqual(parsers.parse_esxcli_smart_get(smart_output)["temperature_c"], 35)
        with mock.patch.object(parsers, "_coerce_int_like", lambda value: 7):
            self.assertEqual(parsers.parse_esxcli_smart_get(smart_output)["temperature_c"], 7)

        # parse_ssh_outputs stayed in parsers and reads the three StorCLI readers there.
        calls: list[str] = []
        outputs = {
            "storcli /c0 show all J": _response({"Basics": {"Model": "x"}}),
            "storcli /c0/vall show all J": _response({}),
            "storcli /c0/eall/sall show all J": _response({}),
        }
        with (
            mock.patch.object(parsers, "parse_storcli_controller_info", lambda output: calls.append("info") or {"m": 1}),
            mock.patch.object(parsers, "parse_storcli_virtual_drives", lambda *args: calls.append("vd") or [{"v": 1}]),
            mock.patch.object(parsers, "parse_storcli_physical_drives", lambda *args: calls.append("pd") or [{"p": 1}]),
            mock.patch.object(parsers, "storcli_physical_drive_list_complete", lambda output: calls.append("ok") or True),
        ):
            parsed = parsers.parse_ssh_outputs(outputs, 24, None)
        self.assertEqual(calls, ["info", "ok", "pd", "vd"])
        self.assertEqual(parsed.esxi_storcli_controller, {"m": 1})
        self.assertEqual(parsed.esxi_storcli_virtual_drives, [{"v": 1}])
        self.assertEqual(parsed.esxi_storcli_physical_drives, [{"p": 1}])
        self.assertEqual(parsed.esxi_storcli_drive_lists, {"storcli /c0/eall/sall show all J": True})

    def test_moved_functions_name_the_new_module(self) -> None:
        import app.services.parsers_storcli as storcli

        for name in MOVED_NAMES:
            value = getattr(storcli, name)
            if isinstance(value, types.FunctionType):
                with self.subTest(name=name):
                    self.assertEqual(value.__module__, "app.services.parsers_storcli")
                    self.assertEqual(value.__qualname__, name)


class StorcliBehaviourPinTests(unittest.TestCase):
    """What the readers return, unchanged by the move."""

    def test_coerce_int_like(self) -> None:
        from app.services.parsers import _coerce_int_like

        cases = ((True, None), (False, None), (3, 3), (4.0, 4), (4.5, None), (" 1,234 C", 1234), ("", None),
                 ("x", None), (None, None), ("-7", -7), ([1], None))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_coerce_int_like(value), expected)

    def test_malformed_controller_blocks_warn_and_are_skipped(self) -> None:
        from app.services.parsers import parse_storcli_physical_drives

        output = json.dumps({"Controllers": [
            1,
            {"Command Status": {"Description": "Bad thing."}, "Response Data": []},
            {"Response Data": {"PD LIST": [{"EID:Slt": "252:1", "State": "Onln"}, 5, {"EID:Slt": "252"}]}},
        ]})
        warnings: list[str] = []
        drives = parse_storcli_physical_drives(output, warnings, "c9")
        self.assertEqual(warnings, [
            "StorCLI controller block 0 was invalid and was not used: controller entry was not an object.",
            "StorCLI controller block 1 was invalid and was not used: Bad thing.",
        ])
        self.assertEqual([(d["slot_key"], d["controller_id"], d["controller_index"]) for d in drives],
                         [("252:1", "c2", 2)])
        self.assertEqual(parse_storcli_physical_drives("{not json"), [])

    def test_census_completeness(self) -> None:
        from app.services.parsers import storcli_physical_drive_list_complete as complete

        listed = {"PD LIST": [{"EID:Slt": "252:1"}], "Drive /c0/e252/s1": {"SN": "a"}}
        self.assertTrue(complete(_response(listed)))
        self.assertFalse(complete(_response(listed, status="Failure")))
        self.assertFalse(complete(_response({"PD LIST": []})))
        self.assertFalse(complete(_response({"PD LIST": [{"EID:Slt": "252:1"}, 5]})))
        self.assertFalse(complete(_response({**listed, "Drive /c0/e252/s2": {"SN": "b"}})))
        self.assertFalse(complete("[]"))

    def test_virtual_drive_sections(self) -> None:
        from app.services.parsers import parse_storcli_virtual_drives

        output = _response({
            "VD LIST": [{"DG/VD": "0/1", "TYPE": "RAID1"}, {"Name": "none"}],
            "PDs for VD 1": [{"EID:Slt": "252:3"}],
            "PDs for VD 10": [{"EID:Slt": "252:4"}],
            "/c0/v1": [{"SCSI NAA Id": "600"}],
        })
        drives = parse_storcli_virtual_drives(output)
        self.assertEqual([(d["vd_id"], d["raid"], d["scsi_naa"]) for d in drives], [("1", "RAID1", "600"), (None, None, None)])
        self.assertEqual([[pd["slot_key"] for pd in d["physical_drives"]] for d in drives], [["252:3"], []])


class StorcliWorkIsStillMeteredTests(unittest.TestCase):
    """The bounded-work meters and the regex sweep still reach the moved code."""

    # (call through a name kept on parsers, result, regex calls, regex work,
    # Python lines), as measured before the StorCLI readers moved to parsers_storcli,
    # then remeasured when the drive parser also read per-drive entries beside a
    # table (#1305) and the detail reader admitted only an entry's own row (#1306).
    @staticmethod
    def _cases() -> tuple:
        from app.services import parsers

        physical = _response({"PD LIST": [{"EID:Slt": "252:1"}], "Drive /c0/e252/s1": {"SN": "a"}})
        virtual = _response({"VD LIST": [{"DG/VD": "0/1"}], "PDs for VD 1": [{"EID:Slt": "252:3"}], "/c0/v1": {}})
        return (
            ("parse_storcli_physical_drives", lambda: len(parsers.parse_storcli_physical_drives(physical)), 1, 8, 1256, 510),
            ("storcli_physical_drive_list_complete",
             lambda: parsers.storcli_physical_drive_list_complete(physical), True, 3, 579, 98),
            ("parse_storcli_virtual_drives", lambda: len(parsers.parse_storcli_virtual_drives(virtual)), 1, 13, 882, 214),
            ("_coerce_int_like", lambda: parsers._coerce_int_like("12 C"), 12, 1, 16, 10),
        )

    def test_calls_through_parsers_meter_the_moved_code(self) -> None:
        from app.services import parsers
        from tests.test_bounded_parser_work import measure

        for name, call, expected, regex_calls, regex_work, python_lines in self._cases():
            with self.subTest(name=name):
                result, meter = measure(parsers, call)
                self.assertEqual(result, expected)
                self.assertEqual(
                    (meter.regex_calls, meter.regex_work, meter.python_lines), (regex_calls, regex_work, python_lines)
                )

    def test_the_pattern_sweep_visits_the_moved_regex_sites(self) -> None:
        from app.services import parsers_storcli
        from tests.test_bounded_parser_work import SWEPT_MODULES, _regex_sites

        self.assertIn(parsers_storcli, SWEPT_MODULES)
        patterns = sorted(site.pattern for site in _regex_sites(parsers_storcli))
        self.assertEqual(patterns, sorted((
            r"-?\d+",
            r"/c(?P<controller>\d+)(?:/|\b)",
            r"/(?P<controller>c\d+)/e(?P<enclosure>\d+)/s(?P<slot>\d+)",
            r"^Drive\s+/c\d+/e\d+/s\d+$",
            r"^Drive\s+/c\d+/e\d+/s\d+$",
            r"/c\d+/v(?P<vd>\d+)",
            r"^VD(?P<vd>\d+)\s+Properties$",
            r"^/c\d+/v\d+$",
            r"\bPDs\s+for\s+VD\s+",
            r"\w*",
        )))


if __name__ == "__main__":
    unittest.main()
