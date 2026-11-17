"""Retained-memory bounds for one entry in a file-backed backup JSON member.

The file-backed validator streams the member, but each mapping entry used to be
built as a complete Python object tree before the model saw it, including
fields the model ignores. One entry holding a large ignored array could then
exhaust the heap while the member stayed far below the archive member-size
limit. These tests check that ignored entry fields are consumed without being
retained, that the fields the model reads are bounded per entry, and that
duplicate-key and syntax validation still cover the skipped values.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from pydantic import BaseModel, ConfigDict, Field

from app.models.domain import ManualMapping
from app.services.slot_detail_store import SlotDetailCacheEntry
from history_service import system_backup as system_backup_module
from history_service.system_backup import (
    BACKUP_GROUP_METADATA,
    MAPPING_FILE_KEY,
    MAX_ARCHIVE_MEMBER_BYTES,
    SystemBackupService,
)
from tests import heap_probe

ENTRY_LIMIT_MESSAGE = "JSON mapping entry exceeds its validation limit."
# The per-entry limits these tests are written against. The first test pins
# them to the module so a change to either is a reviewed change to both.
ENTRY_NODE_LIMIT = 65_536
ENTRY_CHAR_LIMIT = 1024 * 1024

# Peak traced heap allowed while validating a member whose single entry holds
# a 100,000-element ignored array. Validating the small control stays far
# below this; building the ignored array as Python objects needs several times
# more.
IGNORED_ARRAY_HEAP_BUDGET_BYTES = 2 * 1024 * 1024
IGNORED_ARRAY_ELEMENTS = 100_000


def _validate(path: Path, target_key: str, model: Any) -> int:
    return SystemBackupService._validate_streaming_json_member(path, target_key, model)


class _RecordingModel:
    """Model stand-in that records the value handed to it, then validates it."""

    def __init__(self, model: type[BaseModel]) -> None:
        self.model = model
        self.model_fields = model.model_fields
        self.model_config = model.model_config
        self.values: list[Any] = []

    def model_validate(self, value: Any) -> Any:
        self.values.append(value)
        return self.model.model_validate(value)


class FileBackedJsonEntryBoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="json-entry-bounds-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def _write_ignored_array_member(self, name: str, elements: int) -> Path:
        path = self.temp_dir / name
        with path.open("wb") as output:
            output.write(b'{"slot_mappings":{"synthetic":{"slot":0,"ignored":[')
            remaining = elements - 1
            block = b"{}," * 10_000
            while remaining >= 10_000:
                output.write(block)
                remaining -= 10_000
            output.write(b"{}," * remaining)
            output.write(b"{}]}}}")
        return path

    def _measure_peak(self, path: Path) -> tuple[int, int]:
        heap_probe.start()
        try:
            accepted = _validate(path, "slot_mappings", ManualMapping)
            _, peak = heap_probe.get_traced_memory()
        finally:
            heap_probe.stop()
        return accepted, peak

    def test_entry_limits_match_the_validator(self) -> None:
        self.assertEqual(getattr(system_backup_module, "MAX_JSON_ENTRY_NODES", None), ENTRY_NODE_LIMIT)
        self.assertEqual(getattr(system_backup_module, "MAX_JSON_ENTRY_CHARS", None), ENTRY_CHAR_LIMIT)

    def test_ignored_entry_field_is_not_retained_under_heap_budget(self) -> None:
        control = self._write_ignored_array_member("control.json", 1)
        large = self._write_ignored_array_member("large.json", IGNORED_ARRAY_ELEMENTS)
        self.assertLess(large.stat().st_size, MAX_ARCHIVE_MEMBER_BYTES)

        control_accepted, control_peak = self._measure_peak(control)
        large_accepted, large_peak = self._measure_peak(large)

        self.assertEqual(control_accepted, 1)
        self.assertLess(control_peak, IGNORED_ARRAY_HEAP_BUDGET_BYTES)
        self.assertEqual(large_accepted, 1)
        self.assertLess(
            large_peak,
            IGNORED_ARRAY_HEAP_BUDGET_BYTES,
            "validating one entry retained its ignored array",
        )

    def test_model_receives_only_the_fields_it_declares(self) -> None:
        path = self.temp_dir / "ignored-field.json"
        path.write_text(
            '{"slot_mappings":{"synthetic":{"slot":3,"serial":"SANITIZED-A",'
            '"ignored":[{"nested":[1,2,3]}],"also_ignored":"text"}}}',
            encoding="utf-8",
        )
        recorder = _RecordingModel(ManualMapping)

        self.assertEqual(_validate(path, "slot_mappings", recorder), 1)
        self.assertEqual(recorder.values, [{"slot": 3, "serial": "SANITIZED-A"}])

    def test_duplicate_key_inside_ignored_field_is_still_rejected(self) -> None:
        path = self.temp_dir / "ignored-duplicate.json"
        path.write_text(
            '{"slot_mappings":{"synthetic":{"slot":0,"ignored":[{"a":1,"a":2}]}}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate object key"):
            _validate(path, "slot_mappings", ManualMapping)

    def test_duplicate_ignored_field_name_in_entry_is_still_rejected(self) -> None:
        path = self.temp_dir / "ignored-name-duplicate.json"
        path.write_text(
            '{"slot_mappings":{"synthetic":{"slot":0,"ignored":1,"ignored":2}}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate object key"):
            _validate(path, "slot_mappings", ManualMapping)

    def test_malformed_ignored_field_is_still_rejected(self) -> None:
        path = self.temp_dir / "ignored-malformed.json"
        path.write_text(
            '{"slot_mappings":{"synthetic":{"slot":0,"ignored":[1,}}}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "malformed"):
            _validate(path, "slot_mappings", ManualMapping)

    def test_non_object_entry_is_still_rejected(self) -> None:
        path = self.temp_dir / "array-entry.json"
        path.write_text('{"slot_mappings":{"synthetic":[0]}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            _validate(path, "slot_mappings", ManualMapping)

    def test_declared_field_with_too_many_nodes_is_refused(self) -> None:
        limit = ENTRY_NODE_LIMIT
        path = self.temp_dir / "oversized-nodes.json"
        with path.open("w", encoding="utf-8") as output:
            output.write('{"slot_details":{"synthetic":{"slot":0,"slot_fields":{"values":[')
            output.write(",".join("0" for _ in range(limit + 1)))
            output.write("]}}}}")

        with self.assertRaisesRegex(ValueError, ENTRY_LIMIT_MESSAGE):
            _validate(path, "slot_details", SlotDetailCacheEntry)

    def test_declared_field_with_too_many_characters_is_refused(self) -> None:
        limit = ENTRY_CHAR_LIMIT
        piece = "x" * 32_768
        pieces = limit // len(piece) + 1
        path = self.temp_dir / "oversized-chars.json"
        with path.open("w", encoding="utf-8") as output:
            output.write('{"slot_details":{"synthetic":{"slot":0,"slot_fields":{')
            output.write(",".join(f'"k{index}":"{piece}"' for index in range(pieces)))
            output.write("}}}}")

        with self.assertRaisesRegex(ValueError, ENTRY_LIMIT_MESSAGE):
            _validate(path, "slot_details", SlotDetailCacheEntry)

    def test_declared_field_within_bounds_is_accepted(self) -> None:
        path = self.temp_dir / "bounded-slot-detail.json"
        fields = ",".join(f'"field_{index}":"value-{index}"' for index in range(200))
        ports = ",".join(f'{{"phy":{index},"errors":[0,0,0]}}' for index in range(16))
        path.write_text(
            '{"slot_details":{"synthetic":{"slot":0,'
            f'"slot_fields":{{{fields}}},"smart_fields":{{"sas_ports":[{ports}]}}}}}}}}',
            encoding="utf-8",
        )
        self.assertEqual(_validate(path, "slot_details", SlotDetailCacheEntry), 1)

    def test_entry_bounds_reset_between_entries(self) -> None:
        # Each entry has its own budget: many entries that are each well under
        # the limit must not add up to a refusal.
        per_entry = ENTRY_NODE_LIMIT // 4
        values = ",".join("0" for _ in range(per_entry))
        path = self.temp_dir / "many-bounded-entries.json"
        path.write_text(
            '{"slot_details":{'
            + ",".join(
                f'"e{index}":{{"slot":{index},"slot_fields":{{"v":[{values}]}}}}'
                for index in range(6)
            )
            + "}}",
            encoding="utf-8",
        )
        self.assertEqual(_validate(path, "slot_details", SlotDetailCacheEntry), 6)

    def test_duplicate_key_inside_declared_field_is_still_rejected(self) -> None:
        path = self.temp_dir / "declared-duplicate.json"
        path.write_text(
            '{"slot_details":{"synthetic":{"slot":0,"slot_fields":{"a":1,"a":2}}}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate object key"):
            _validate(path, "slot_details", SlotDetailCacheEntry)

    def test_aliased_field_is_passed_to_the_model(self) -> None:
        class AliasedModel(BaseModel):
            slot: int = Field(alias="bay")

        path = self.temp_dir / "aliased.json"
        path.write_text(
            '{"entries":{"synthetic":{"bay":4,"ignored":[1]}}}',
            encoding="utf-8",
        )
        recorder = _RecordingModel(AliasedModel)
        self.assertEqual(_validate(path, "entries", recorder), 1)
        self.assertEqual(recorder.values, [{"bay": 4}])

    def test_model_that_forbids_extra_fields_still_sees_them(self) -> None:
        class StrictModel(BaseModel):
            model_config = ConfigDict(extra="forbid")
            slot: int

        path = self.temp_dir / "strict.json"
        path.write_text(
            '{"entries":{"synthetic":{"slot":0,"unexpected":1}}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "unexpected"):
            _validate(path, "entries", StrictModel)

    def test_import_preflight_refuses_oversized_mapping_entry(self) -> None:
        limit = ENTRY_NODE_LIMIT
        path = self.temp_dir / "oversized-mapping.json"
        with path.open("w", encoding="utf-8") as output:
            output.write('{"slot_mappings":{"synthetic":{"slot":0,"notes":[')
            output.write(",".join("0" for _ in range(limit + 1)))
            output.write("]}}}")
        manifest = {
            "groups": [
                {
                    "key": MAPPING_FILE_KEY,
                    "selected": True,
                    "present": True,
                    "restore_mode": "file",
                }
            ],
            "files": [
                {
                    "key": MAPPING_FILE_KEY,
                    "group_key": MAPPING_FILE_KEY,
                    "archive_path": str(
                        BACKUP_GROUP_METADATA[MAPPING_FILE_KEY]["archive_root"]
                    ),
                }
            ],
        }
        service = SystemBackupService.__new__(SystemBackupService)

        with self.assertRaisesRegex(
            ValueError, "selected mapping_file member is invalid"
        ) as raised:
            service._preflight_import_members(
                manifest,
                service._manifest_group_entries(manifest),
                {MAPPING_FILE_KEY: path},
            )
        self.assertEqual(str(raised.exception.__cause__), ENTRY_LIMIT_MESSAGE)


class BackupJsonModuleSplitTests(unittest.TestCase):
    """The streaming reader lives in backup_json; system_backup keeps its names."""

    MOVED_PLAIN_NAMES = (
        "JSON_STREAM_CHUNK_CHARS",
        "MAX_JSON_KEY_CHARS",
        "JSON_DUPLICATE_CACHE_KIB",
        "MAX_JSON_ENTRY_NODES",
        "MAX_JSON_ENTRY_CHARS",
        "JSON_ENTRY_LIMIT_MESSAGE",
        "_DiskBackedDuplicateKeyTracker",
        "_model_entry_field_names",
    )

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="json-split-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.member = self.temp_dir / "mapping.json"
        self.member.write_text(
            '{"slot_mappings":{"first":{"slot":0,"ignored":[1]},"second":{"slot":1}}}',
            encoding="utf-8",
        )

    def test_backup_json_imports_without_system_backup(self) -> None:
        script = (
            "import sys\n"
            "import history_service.backup_json as moved\n"
            "assert 'history_service.system_backup' not in sys.modules, 'system_backup was imported'\n"
            "print(moved._StreamingJSONReader.__module__)\n"
        )
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "history_service.backup_json")

    def test_system_backup_keeps_the_moved_names(self) -> None:
        backup_json = importlib.import_module("history_service.backup_json")
        for name in self.MOVED_PLAIN_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(system_backup_module, name), getattr(backup_json, name))
        self.assertTrue(
            issubclass(system_backup_module._StreamingJSONReader, backup_json._StreamingJSONReader)
        )

    def test_replaced_reader_on_system_backup_is_used(self) -> None:
        real_reader = system_backup_module._StreamingJSONReader
        created: list[Any] = []

        class RecordingReader(real_reader):  # type: ignore[misc, valid-type]
            def __init__(self, source: Any) -> None:
                super().__init__(source)
                created.append(self)

        with mock.patch.object(system_backup_module, "_StreamingJSONReader", RecordingReader):
            self.assertEqual(_validate(self.member, "slot_mappings", ManualMapping), 2)
        self.assertEqual(len(created), 1)

    def test_replaced_tracker_on_system_backup_is_used(self) -> None:
        real_tracker = system_backup_module._DiskBackedDuplicateKeyTracker
        entered: list[Any] = []

        class RecordingTracker(real_tracker):  # type: ignore[misc, valid-type]
            __slots__ = ()

            def __enter__(self) -> Any:
                entered.append(self)
                return super().__enter__()

        with mock.patch.object(system_backup_module, "_DiskBackedDuplicateKeyTracker", RecordingTracker):
            self.assertEqual(_validate(self.member, "slot_mappings", ManualMapping), 2)
        self.assertEqual(len(entered), 1)

    def test_replaced_field_name_helper_on_system_backup_is_used(self) -> None:
        real_helper = system_backup_module._model_entry_field_names
        seen: list[Any] = []

        def recording_helper(model: Any) -> frozenset[str] | None:
            seen.append(model)
            return real_helper(model)

        recorder = _RecordingModel(ManualMapping)
        with mock.patch.object(system_backup_module, "_model_entry_field_names", recording_helper):
            self.assertEqual(_validate(self.member, "slot_mappings", recorder), 2)
        self.assertEqual(seen, [recorder])
        self.assertEqual(recorder.values, [{"slot": 0}, {"slot": 1}])


if __name__ == "__main__":
    unittest.main()
