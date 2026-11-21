"""Whole-file read budget for the JSON state files a backup restores (#1286).

The slot detail cache, the SAS Fabric alias store and the mapping store are
restored from a backup as the archive held them, then read back in one piece.
Backup validation bounds one entry at a time, but many small values cost far
more as Python objects than as text, so a file whose entries each pass that
check could still build many times its size when a store reads it. These tests
check that every store refuses a file over the whole-file budget while reading
it, before building it, that backup validation refuses the same file with a
message naming the member, and that a realistic large installation still loads
and passes validation.
"""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from app.models.domain import SasFabricAlias
from app.services.mapping_store import MappingScopeConflict, MappingStore
from app.services.sas_fabric_alias_store import SasFabricAliasStore
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore
from history_service.system_backup import (
    BACKUP_GROUP_METADATA,
    MAPPING_FILE_KEY,
    SAS_FABRIC_ALIAS_FILE_KEY,
    SLOT_DETAIL_FILE_KEY,
    SystemBackupService,
)
from tests import heap_probe

# The budget these tests are written against. The first test pins it to the
# module so a change to either is a reviewed change to both.
FILE_BYTE_LIMIT = 64 * 1024 * 1024
FILE_ITEM_LIMIT = 4 * 1024 * 1024
# Installation size the budget must admit: sixteen fully populated
# 1,024-disk expanders of the largest slot detail entry the inventory writes.
DESIGN_BAYS = 16 * 1024
# Realistic control: several thousand bays with full slot and SMART fields.
CONTROL_BAYS = 4096
# Values per entry in the issue's second member: every entry stays inside the
# per-entry backup limits (65,536 values, 1 MiB of text).
VALUES_PER_ENTRY = 60_000
# Heap allowed above the file's own size while a store refuses it. Refusing
# keeps at most the bytes already read; building the file needs many times
# its size.
REFUSAL_HEAP_SLACK_BYTES = 4 * 1024 * 1024


def _budget_module() -> Any:
    from app.services import json_state_budget

    return json_state_budget


def _realistic_entry(index: int) -> SlotDetailCacheEntry:
    """The largest slot detail entry the inventory writes, synthetic values."""
    serial = f"SYN{index:012d}"
    lunid = f"5000c500{index:08x}"
    sas = f"5000c5{index:010x}"
    enclosure = f"5000c50000{index // 1024:06x}"
    return SlotDetailCacheEntry(
        system_id=f"system-{index // 1024:04d}",
        enclosure_id=enclosure,
        slot=index % 1024,
        identifiers=[f"serial:{serial}", f"lunid:{lunid}", f"gptid:{index:08x}-0000-11ee-8000-000000000000", f"sas:{sas}"],
        slot_fields={
            "device_name": f"da{index}",
            "smart_device_names": [f"da{index}", f"da{index + 1}", f"pass{index}"],
            "smart_device_type": "scsi",
            "serial": serial,
            "model": "SYNTHETIC-MODEL-20TB-SAS-12G",
            "size_bytes": 20_000_588_955_648,
            "size_human": "18.19 TiB",
            "gptid": f"gptid/{index:08x}-0000-11ee-8000-000000000000",
            "persistent_id_label": f"disk-label-{index:08x}",
            "multipath": {
                "name": f"disk{index}",
                "device_name": f"multipath/disk{index}",
                "uuid": f"{index:08x}-1111-2222-3333-444444444444",
                "mode": "ACTIVE/PASSIVE",
                "state": "OPTIMAL",
                "provider_state": "ACTIVE",
                "path_device_name": f"da{index}",
                "alternate_path_device": f"da{index + 1}",
                "lunid": lunid,
                "bus": "SAS",
                "members": [
                    {"device_name": f"da{index}", "state": "ACTIVE", "mode": "ACTIVE", "controller_label": "Controller A"},
                    {"device_name": f"da{index + 1}", "state": "PASSIVE", "mode": "PASSIVE", "controller_label": "Controller B"},
                ],
            },
            "logical_block_size": 512,
            "physical_block_size": 4096,
            "logical_unit_id": lunid,
            "sas_address": sas,
            "enclosure_id": enclosure,
            "enclosure_label": "Front shelf 01",
            "enclosure_name": "Synthetic 102-bay JBOD",
            "enclosure_identifier": enclosure,
        },
        smart_fields={
            "logical_block_size": 512,
            "physical_block_size": 4096,
            "power_on_hours": 43_210,
            "power_on_days": 1_800,
            "rotation_rate_rpm": 7_200,
            "form_factor": "3.5 inches",
            "firmware_version": "SYN4",
            "protocol_version": "SPC-5",
            "read_cache_enabled": True,
            "writeback_cache_enabled": False,
            "trim_supported": False,
            "transport_protocol": "SAS (SPL-4)",
            "logical_unit_id": lunid,
            "sas_address": sas,
            "attached_sas_address": f"5000c6{index:010x}",
            "negotiated_link_rate": "12.0 Gbps",
            "unread_fields": ["form_factor", "trim_supported"],
            "available": True,
        },
        smart_updated_at="2026-01-01T00:00:00+00:00",
        collection_binding="0" * 64,
    )


def _counted_items(data: bytes) -> int:
    return 1 + sum(data.count(marker) for marker in (b",", b":", b"{", b"["))


def _write_bulk(path: Path, head: bytes, values: int, tail: bytes, item: bytes = b"0.5") -> None:
    """Write ``head``, ``values`` comma-separated copies of ``item``, ``tail``."""
    block_count = 10_000
    block = (item + b",") * block_count
    with path.open("wb") as output:
        output.write(head)
        remaining = values - 1
        while remaining >= block_count:
            output.write(block)
            remaining -= block_count
        output.write((item + b",") * remaining)
        output.write(item)
        output.write(tail)


def _write_many_entry_slot_details(path: Path, entries: int) -> None:
    """The issue's second member: bulk in declared fields, each entry in limits."""
    values = b",".join([b"0.5"] * VALUES_PER_ENTRY)
    with path.open("wb") as output:
        output.write(b'{"version":1,"slot_details":{')
        for index in range(entries):
            if index:
                output.write(b",")
            output.write(
                b'"system-a:enclosure-a:%d":{"system_id":"system-a","enclosure_id":"enclosure-a",'
                b'"slot":%d,"slot_fields":{"values":[' % (index, index)
            )
            output.write(values)
            output.write(b"]}}")
        output.write(b"}}")


def _write_padded(path: Path, document: bytes, total_bytes: int) -> None:
    """A valid document followed by whitespace up to ``total_bytes``."""
    with path.open("wb") as output:
        output.write(document)
        remaining = total_bytes - len(document)
        block = b" " * (1024 * 1024)
        while remaining >= len(block):
            output.write(block)
            remaining -= len(block)
        output.write(b" " * remaining)


SLOT_DETAIL_ONE_ENTRY_HEAD = (
    b'{"version":1,"slot_details":{"system-a:enclosure-a:0":{"system_id":"system-a",'
    b'"enclosure_id":"enclosure-a","slot":0,"serial_history":['
)
SLOT_DETAIL_ONE_ENTRY_TAIL = b"]}}}"
SLOT_DETAIL_SMALL = (
    b'{"version":1,"slot_details":{"system-a:enclosure-a:0":{"system_id":"system-a",'
    b'"enclosure_id":"enclosure-a","slot":0,"slot_fields":{"serial":"SYN-0001"}}}}'
)
SAS_ALIAS_HEAD = (
    b'{"version":1,"sas_fabric_aliases":{"system-a:system:expander-1":{"system_id":"system-a",'
    b'"object_id":"expander-1","label":"Shelf A"}},"padding":['
)
SAS_ALIAS_SMALL = (
    b'{"version":1,"sas_fabric_aliases":{"system-a:system:expander-1":{"system_id":"system-a",'
    b'"object_id":"expander-1","label":"Shelf A"}}}'
)
MAPPING_HEAD = (
    b'{"version":1,"slot_mappings":{"system-a:enclosure-a:0":{"system_id":"system-a",'
    b'"enclosure_id":"enclosure-a","slot":0,"serial":"SYN-0001"}},"padding":['
)
MAPPING_SMALL = (
    b'{"version":1,"slot_mappings":{"system-a:enclosure-a:0":{"system_id":"system-a",'
    b'"enclosure_id":"enclosure-a","slot":0,"serial":"SYN-0001"}}}'
)
PADDING_TAIL = b"]}"


def _preflight(group_key: str, content: Path | bytes) -> None:
    manifest = {
        "groups": [{"key": group_key, "selected": True, "present": True, "restore_mode": "file"}],
        "files": [
            {
                "key": group_key,
                "group_key": group_key,
                "archive_path": str(BACKUP_GROUP_METADATA[group_key]["archive_root"]),
            }
        ],
    }
    service = SystemBackupService.__new__(SystemBackupService)
    service._preflight_import_members(manifest, service._manifest_group_entries(manifest), {group_key: content})


class _TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="json-state-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def _peak_while(self, action: Any) -> tuple[Any, int]:
        heap_probe.start()
        try:
            result = action()
            _, peak = heap_probe.get_traced_memory()
        finally:
            heap_probe.stop()
        return result, peak


class JsonStateBudgetSizingTests(_TempDirTestCase):
    def test_budget_matches_the_module(self) -> None:
        budget = _budget_module()
        self.assertEqual(budget.MAX_JSON_STATE_FILE_BYTES, FILE_BYTE_LIMIT)
        self.assertEqual(budget.MAX_JSON_STATE_FILE_ITEMS, FILE_ITEM_LIMIT)

    def test_budget_admits_the_design_installation_of_the_largest_entry(self) -> None:
        store = SlotDetailStore(str(self.temp_dir / "slot_detail_cache.json"))
        sample = 64
        store.save_entries([_realistic_entry(index) for index in range(sample)])
        data = store.file_path.read_bytes()
        bytes_per_bay = len(data) / sample
        items_per_bay = _counted_items(data) / sample
        self.assertGreater(bytes_per_bay, 2_500, "the sample is not the largest entry")
        self.assertLessEqual(DESIGN_BAYS * bytes_per_bay, FILE_BYTE_LIMIT)
        self.assertLessEqual(DESIGN_BAYS * items_per_bay, FILE_ITEM_LIMIT)

    def test_realistic_large_installation_loads_and_passes_backup_validation(self) -> None:
        store = SlotDetailStore(str(self.temp_dir / "slot_detail_cache.json"))
        store.save_entries([_realistic_entry(index) for index in range(CONTROL_BAYS)])
        self.assertEqual(len(store.load_all()), CONTROL_BAYS)
        _preflight(SLOT_DETAIL_FILE_KEY, store.file_path.read_bytes())

    def test_realistic_file_backed_member_passes_backup_validation(self) -> None:
        # The file-backed validator parses in Python one character at a time,
        # so this control uses fewer bays than the in-memory one above.
        store = SlotDetailStore(str(self.temp_dir / "slot_detail_cache.json"))
        store.save_entries([_realistic_entry(index) for index in range(256)])
        _preflight(SLOT_DETAIL_FILE_KEY, store.file_path)


class SlotDetailStoreBudgetTests(_TempDirTestCase):
    def _assert_refused_within_heap(self, path: Path, ceiling: int) -> None:
        store = SlotDetailStore(str(path))
        with _captured_warnings("app.services.slot_detail_store") as warnings:
            loaded, peak = self._peak_while(store.load_all)
        self.assertLess(peak, ceiling, "the store built an over-budget file")
        self.assertEqual(loaded, {})
        self.assertTrue(any(path.name in message for message in warnings), warnings)

    def test_entries_each_within_entry_limits_but_over_file_budget_are_refused(self) -> None:
        path = self.temp_dir / "slot_detail_cache.json"
        _write_many_entry_slot_details(path, FILE_ITEM_LIMIT // VALUES_PER_ENTRY + 1)
        self._assert_refused_within_heap(path, path.stat().st_size + REFUSAL_HEAP_SLACK_BYTES)

    def test_one_entry_with_bulk_in_an_ignored_field_is_refused(self) -> None:
        path = self.temp_dir / "slot_detail_cache.json"
        _write_bulk(path, SLOT_DETAIL_ONE_ENTRY_HEAD, FILE_ITEM_LIMIT, SLOT_DETAIL_ONE_ENTRY_TAIL)
        self._assert_refused_within_heap(path, path.stat().st_size + REFUSAL_HEAP_SLACK_BYTES)

    def test_file_over_byte_budget_is_refused_without_reading_it(self) -> None:
        path = self.temp_dir / "slot_detail_cache.json"
        _write_padded(path, SLOT_DETAIL_SMALL, FILE_BYTE_LIMIT + 1)
        self._assert_refused_within_heap(path, REFUSAL_HEAP_SLACK_BYTES)

    def test_small_file_control_loads(self) -> None:
        path = self.temp_dir / "slot_detail_cache.json"
        path.write_bytes(SLOT_DETAIL_SMALL)
        with _captured_warnings("app.services.slot_detail_store") as warnings:
            loaded = SlotDetailStore(str(path)).load_all()
        self.assertEqual(list(loaded), ["system-a:enclosure-a:0"])
        self.assertEqual(warnings, [])


class SasFabricAliasStoreBudgetTests(_TempDirTestCase):
    def test_store_over_item_budget_is_refused_and_left_unchanged(self) -> None:
        path = self.temp_dir / "sas_fabric_aliases.json"
        _write_bulk(path, SAS_ALIAS_HEAD, FILE_ITEM_LIMIT, PADDING_TAIL)
        before = path.stat()
        store = SasFabricAliasStore(path)
        with _captured_warnings("app.services.sas_fabric_alias_store") as warnings:
            self.assertEqual(store.load_all(), {})
            with self.assertRaisesRegex(ValueError, "sas_fabric_aliases.json is over the limit") as raised:
                store.save_alias(SasFabricAlias(system_id="system-a", object_id="expander-2", label="Shelf B"))
        self.assertIn("No changes were saved", str(raised.exception))
        self.assertNotIn(str(self.temp_dir), str(raised.exception))
        self.assertEqual(len(warnings), 2)
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns), (after.st_ino, after.st_size, after.st_mtime_ns))

    def test_small_store_control_saves(self) -> None:
        path = self.temp_dir / "sas_fabric_aliases.json"
        path.write_bytes(SAS_ALIAS_SMALL)
        store = SasFabricAliasStore(path)
        store.save_alias(SasFabricAlias(system_id="system-a", object_id="expander-2", label="Shelf B"))
        self.assertEqual(len(store.load_all()), 2)


class MappingStoreBudgetTests(_TempDirTestCase):
    def test_store_over_item_budget_is_refused_by_strict_and_tolerant_reads(self) -> None:
        path = self.temp_dir / "slot_mappings.json"
        _write_bulk(path, MAPPING_HEAD, FILE_ITEM_LIMIT, PADDING_TAIL)
        store = MappingStore(path)
        with _captured_warnings("app.services.mapping_store") as warnings:
            self.assertEqual(dict(store.load_all()), {})
            with self.assertRaises(MappingScopeConflict):
                store.load_read_snapshot()
        self.assertEqual(len(warnings), 2)
        self.assertIn("slot_mappings.json", warnings[0])

    def test_store_over_byte_budget_is_refused_without_reading_it(self) -> None:
        path = self.temp_dir / "slot_mappings.json"
        _write_padded(path, MAPPING_SMALL, FILE_BYTE_LIMIT + 1)
        store = MappingStore(path)
        with _captured_warnings("app.services.mapping_store") as warnings:
            loaded, peak = self._peak_while(lambda: dict(store.load_all()))
        self.assertLess(peak, REFUSAL_HEAP_SLACK_BYTES)
        self.assertEqual(loaded, {})
        self.assertEqual(len(warnings), 1)

    def test_small_store_control_loads(self) -> None:
        path = self.temp_dir / "slot_mappings.json"
        path.write_bytes(MAPPING_SMALL)
        self.assertEqual(len(MappingStore(path).load_all()), 1)


class BackupValidationBudgetTests(_TempDirTestCase):
    def _assert_preflight_refuses(self, group_key: str, content: Path | bytes) -> None:
        archive_path = str(BACKUP_GROUP_METADATA[group_key]["archive_root"])
        with self.assertRaisesRegex(ValueError, f"Backup bundle member {archive_path} is over the limit"):
            _preflight(group_key, content)

    def test_file_backed_slot_detail_member_over_item_budget_is_refused(self) -> None:
        path = self.temp_dir / "member.json"
        _write_many_entry_slot_details(path, FILE_ITEM_LIMIT // VALUES_PER_ENTRY + 1)
        self._assert_preflight_refuses(SLOT_DETAIL_FILE_KEY, path)

    def test_file_backed_member_over_byte_budget_is_refused_for_each_group(self) -> None:
        for group_key, document in (
            (SLOT_DETAIL_FILE_KEY, SLOT_DETAIL_SMALL),
            (SAS_FABRIC_ALIAS_FILE_KEY, SAS_ALIAS_SMALL),
            (MAPPING_FILE_KEY, MAPPING_SMALL),
        ):
            with self.subTest(group_key=group_key):
                path = self.temp_dir / f"{group_key}.json"
                _write_padded(path, document, FILE_BYTE_LIMIT + 1)
                self._assert_preflight_refuses(group_key, path)

    def test_in_memory_members_over_item_budget_are_refused_for_each_group(self) -> None:
        for group_key, head, tail in (
            (SLOT_DETAIL_FILE_KEY, SLOT_DETAIL_ONE_ENTRY_HEAD, SLOT_DETAIL_ONE_ENTRY_TAIL),
            (SAS_FABRIC_ALIAS_FILE_KEY, SAS_ALIAS_HEAD, PADDING_TAIL),
            (MAPPING_FILE_KEY, MAPPING_HEAD, PADDING_TAIL),
        ):
            with self.subTest(group_key=group_key):
                path = self.temp_dir / f"{group_key}.json"
                _write_bulk(path, head, FILE_ITEM_LIMIT, tail)
                self._assert_preflight_refuses(group_key, path.read_bytes())

    def test_small_members_are_accepted_for_each_group(self) -> None:
        for group_key, document in (
            (SLOT_DETAIL_FILE_KEY, SLOT_DETAIL_SMALL),
            (SAS_FABRIC_ALIAS_FILE_KEY, SAS_ALIAS_SMALL),
            (MAPPING_FILE_KEY, MAPPING_SMALL),
        ):
            with self.subTest(group_key=group_key):
                path = self.temp_dir / f"{group_key}.json"
                path.write_bytes(document)
                _preflight(group_key, path)
                _preflight(group_key, document)

    def test_member_check_retains_no_more_than_one_read_chunk(self) -> None:
        path = self.temp_dir / "member.json"
        _write_bulk(path, SLOT_DETAIL_ONE_ENTRY_HEAD, FILE_ITEM_LIMIT, SLOT_DETAIL_ONE_ENTRY_TAIL)
        budget = _budget_module()

        def check() -> None:
            with self.assertRaises(budget.JsonStateFileTooLarge):
                SystemBackupService._check_json_state_member(path, "data/slot_detail_cache.json")

        _, peak = self._peak_while(check)
        self.assertLess(peak, REFUSAL_HEAP_SLACK_BYTES)


class LoadAndCheckAgreeTests(_TempDirTestCase):
    """A file the store refuses is the file backup validation refuses."""

    def _verdicts(self, data: bytes) -> tuple[bool, bool, bool]:
        path = self.temp_dir / "slot_detail_cache.json"
        path.write_bytes(data)
        with _quiet_store_log():
            store_loaded = bool(SlotDetailStore(str(path)).load_all())
        try:
            _preflight(SLOT_DETAIL_FILE_KEY, path)
            file_checked = True
        except ValueError:
            file_checked = False
        try:
            _preflight(SLOT_DETAIL_FILE_KEY, data)
            bytes_checked = True
        except ValueError:
            bytes_checked = False
        return store_loaded, file_checked, bytes_checked

    def test_boundary_is_the_same_for_load_and_check(self) -> None:
        budget = _budget_module()
        data = SLOT_DETAIL_SMALL
        items = _counted_items(data)
        cases = (
            ("bytes at limit", "MAX_JSON_STATE_FILE_BYTES", len(data), True),
            ("bytes one over", "MAX_JSON_STATE_FILE_BYTES", len(data) - 1, False),
            ("items at limit", "MAX_JSON_STATE_FILE_ITEMS", items, True),
            ("items one over", "MAX_JSON_STATE_FILE_ITEMS", items - 1, False),
        )
        for label, name, limit, accepted in cases:
            with self.subTest(label), mock.patch.object(budget, name, limit):
                self.assertEqual(self._verdicts(data), (accepted, accepted, accepted))

    def test_every_counted_marker_counts(self) -> None:
        budget = _budget_module()
        for marker, document in (
            (",", b'{"a":[1,2]}'),
            (":", b'{"a":1}'),
            ("{", b'{"a":{}}'),
            ("[", b'{"a":[]}'),
        ):
            with self.subTest(marker=marker):
                items = _counted_items(document)
                with mock.patch.object(budget, "MAX_JSON_STATE_FILE_ITEMS", items - 1):
                    with self.assertRaises(budget.JsonStateFileTooLarge):
                        budget.check_json_state(io.BytesIO(document), "state.json")
                with mock.patch.object(budget, "MAX_JSON_STATE_FILE_ITEMS", items):
                    budget.check_json_state(io.BytesIO(document), "state.json")


@contextmanager
def _quiet_store_log() -> Iterator[None]:
    """Silence the store's refusal warning in the boundary table."""
    logger = logging.getLogger("app.services.slot_detail_store")
    previous = logger.disabled
    logger.disabled = True
    try:
        yield
    finally:
        logger.disabled = previous


class _ListHandler(logging.Handler):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(logging.WARNING)
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _captured_warnings(name: str) -> Iterator[list[str]]:
    """Collect warnings without failing when there are none."""
    messages: list[str] = []
    handler = _ListHandler(messages)
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
