"""A bay whose occupancy was not read is unknown in every consumer (#1218).

The slot view keeps the legacy ``present`` boolean (False for an unread bay)
and says separately whether occupancy was read: ``presence_known``. Consumers
read that fact instead of treating the legacy False as an empty bay:

- a status keyword such as ``OK`` does not turn an unread SES bay occupied;
- the SES storage-view runtime does not mark an unread bay occupied;
- history keeps the last recorded occupancy and disk for an unread bay, writes
  no present-to-absent event for it, and does not call a SMART pass complete
  while the disk last recorded there was not read; earlier alert evidence is
  kept until a real read replaces it.

Every consumer asks one reader, ``slot_occupancy`` (``slotOccupancy`` on the
page), which answers present, empty or unknown. A maintained scan keeps direct
reads of the occupancy fields to a short allowlist with a reason each.

Real parsers, resolvers, slot-view builders, the history collector and a
disposable SQLite history store; synthetic inputs only.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import StorageViewConfig
from app.models import domain as domain_models
from app.models.domain import InventorySnapshot, ManualMapping, SlotView, StorageViewRuntimeSlot
from app.services import inventory as inventory_module
from app.services.inventory_accounting import build_disk_retention_accounting
from app.services.sas_fabric import build_sas_fabric_snapshot
from history_service.collector import HistoryCollector, ScopeSnapshot
from history_service.config import HistorySettings
from history_service.domain import SlotStateRecord
from history_service.store import HistoryStore
# Module imports, not class imports: a TestCase class bound in this module
# would be collected and run a second time here.
from tests import test_quantastor_ses_projection as quantastor_cases
from tests import test_ses_unknown_presence as presence_cases

MAPPING = ManualMapping(slot=0, serial="SANITIZED-MAPPED")
FAILED_HOT = {0: {"available": True, "smart_health_status": "FAILED", "temperature_c": 61}}
PASSED_COOL = {0: {"available": True, "smart_health_status": "PASSED", "temperature_c": 35}}


def _core_view(row: str, mapping: ManualMapping | None = None, disk: Any = None) -> SlotView:
    case = presence_cases.CoreSesutilShowPresenceTests()
    try:
        _resolution, view = case.resolve(case.service(), presence_cases._show_candidate(row), mapping, disk)
    finally:
        case.doCleanups()
    return view


def _quantastor_unknown_view(mapped: bool) -> SlotView:
    case = quantastor_cases.QuantastorSesProjectionTests()
    try:
        service = case.service()
        if mapped:
            service.mapping_store.get_mapping.return_value = MAPPING  # type: ignore[attr-defined]
        overlay, _candidate = quantastor_cases._overlay("unknown-presence")
        return case.bay_zero_without_api_disk(service, overlay)
    finally:
        case.doCleanups()


def _payload(view: SlotView) -> dict[str, Any]:
    # The collector reads the inventory API's JSON, not the model object.
    payload = json.loads(view.model_dump_json())
    payload["enclosure_id"] = "enc-a"
    return payload


KNOWN_PRESENT = lambda: _core_view("Slot 00  da0  SANITIZED-MODEL  SANITIZED-MAPPED  OK", MAPPING, presence_cases._disk())  # noqa: E731
KNOWN_EMPTY = lambda: _core_view("Slot 00  -  -  -  Not installed")  # noqa: E731
UNREAD = {
    "core-show-unknown": lambda: _core_view("Slot 00  -  -  -  Unknown"),
    "core-show-ok": lambda: _core_view("Slot 00  -  -  -  OK"),
    "quantastor-unknown": lambda: _quantastor_unknown_view(mapped=False),
    "quantastor-unresolved-mapping": lambda: _quantastor_unknown_view(mapped=True),
}


class SlotViewOccupancyTests(unittest.TestCase):
    def test_unread_bays_are_neither_occupied_nor_empty(self) -> None:
        for name, build in UNREAD.items():
            with self.subTest(name):
                view = build()
                self.assertIs(view.presence_known, False)
                self.assertIs(view.present, False)
                self.assertNotIn(view.state.value, {"empty", "healthy"})
                self.assertIs(_payload(view)["presence_known"], False)
                record = SlotStateRecord.from_snapshot_slot({"selected_system_id": "s"}, _payload(view))
                self.assertIs(record.presence_known, False)

    def test_status_ok_does_not_make_an_unread_ses_bay_occupied(self) -> None:
        # `Slot 00 - - - OK`: no device name, no disk. The parser left
        # occupancy unknown; the OK keyword must not decide it afterwards.
        view = _core_view("Slot 00  -  -  -  OK")
        self.assertIs(view.present, False)
        self.assertIs(view.presence_known, False)
        self.assertEqual(view.state.value, "unknown")

    def test_decided_bays_keep_known_occupancy(self) -> None:
        present = KNOWN_PRESENT()
        self.assertEqual((present.present, present.presence_known, present.state.value), (True, True, "healthy"))
        empty = KNOWN_EMPTY()
        self.assertEqual((empty.present, empty.presence_known, empty.state.value), (False, True, "empty"))
        # Known occupancy is the default, so it is left out of the JSON and
        # saved snapshots without the field read as before.
        self.assertNotIn("presence_known", _payload(present))
        self.assertNotIn("presence_known", _payload(empty))
        self.assertIs(SlotView(slot=0, slot_label="00", row_index=0, column_index=0).presence_known, True)

    def test_status_ok_without_ses_evidence_still_reads_occupied(self) -> None:
        # Control: an appliance status with no SES record behind it keeps its
        # meaning, so the guard is limited to undecided SES occupancy.
        case = presence_cases.CoreSesutilShowPresenceTests()
        try:
            service = case.service()
            from app.services.parsers import parse_ssh_outputs
            view = service._build_slot_view(
                0, 0, 0, {}, {"status": "OK"}, None, None, parse_ssh_outputs({}, 1, None), {}, set(),
            )
        finally:
            case.doCleanups()
        self.assertIs(view.present, True)
        self.assertIs(view.presence_known, True)

    def test_ses_storage_view_runtime_does_not_mark_an_unread_bay_occupied(self) -> None:
        case = presence_cases.CoreSesutilShowPresenceTests()
        try:
            service = case.service()
            view = StorageViewConfig(id="front", label="Front", kind="ses_enclosure", template_id="ses-auto")
            for name, build in {**UNREAD, "known-present": KNOWN_PRESENT, "known-empty": KNOWN_EMPTY}.items():
                with self.subTest(name):
                    slot = build()
                    snapshot = InventorySnapshot(slots=[slot], refresh_interval_seconds=60)
                    runtime = service._build_ses_storage_view_runtime(view, snapshot)
                    runtime_slot = next(item for item in runtime.slots if item.slot_index == 0)
                    self.assertIs(runtime_slot.occupied, name == "known-present")
                    self.assertEqual(runtime_slot.state, slot.state.value)
                    # The page draws a runtime tile from this flag when the
                    # view is backed by another enclosure than the snapshot.
                    unread = name in UNREAD
                    self.assertIs(runtime_slot.presence_known, not unread)
                    self.assertIs("presence_known" in json.loads(runtime_slot.model_dump_json()), unread)
        finally:
            case.doCleanups()


class UnreadBayHistoryTests(unittest.TestCase):
    def collector(self) -> tuple[HistoryCollector, HistoryStore]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = HistoryStore(str(root / "history.db"))
        store.create_backup = MagicMock(return_value=None)  # type: ignore[method-assign]
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(root / "history.db"),
                backup_dir=str(root / "backups"),
                poll_interval_seconds=300,
                failure_backoff_max_seconds=900,
                startup_grace_seconds=0,
            ),
            store,
        )
        return collector, store

    def run_pass(self, collector: HistoryCollector, view: SlotView, summaries: dict, at: datetime) -> tuple[dict, int]:
        collector._enumerate_scopes = AsyncMock(return_value=[ScopeSnapshot(  # type: ignore[method-assign]
            system_id="s", system_label="S", enclosure_id="enc-a", enclosure_label="Shelf",
            snapshot={"selected_system_id": "s", "selected_enclosure_id": "enc-a", "slots": [_payload(view)]},
        )])
        fetch = AsyncMock(return_value=summaries)
        collector._fetch_smart_summaries = fetch  # type: ignore[method-assign]
        with patch("history_service.collector.utcnow", return_value=at):
            asyncio.run(collector.run_once(force_fast=True, include_due_intervals=False))
        return collector.status(), fetch.await_count

    @staticmethod
    def presence_changes(store: HistoryStore) -> list[tuple[Any, Any]]:
        changes = []
        for event in reversed(store.list_slot_events("s", "enc-a", 0)):
            details = json.loads(event.get("details_json") or "{}")
            if "present" in details:
                changes.append((details["present"]["previous"], details["present"]["current"]))
        return changes

    def test_unread_bay_keeps_its_recorded_disk_and_alert_evidence(self) -> None:
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        for name, build in UNREAD.items():
            with self.subTest(name):
                collector, store = self.collector()
                first, _calls = self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
                self.assertEqual(first["last_smart_failure_evidence_disks"], 1)
                unread, calls = self.run_pass(collector, build(), {}, start + timedelta(minutes=5))
                self.assertEqual(calls, 0)
                # The failed disk was not read again: its evidence stands.
                self.assertEqual(unread["last_smart_failure_evidence_disks"], 1)
                self.assertEqual(unread["last_max_temperature_celsius"], 61)
                self.assertEqual(unread["last_smart_failure_evidence_at"], first["last_smart_failure_evidence_at"])
                self.assertEqual(unread["last_temperature_evidence_at"], first["last_temperature_evidence_at"])
                row = store.get_slot_states("s", "enc-a")[0]
                self.assertIs(row.present, True)
                self.assertEqual(row.serial, "SANITIZED-MAPPED")
                self.assertEqual(self.presence_changes(store), [])

    def test_unread_then_known_empty_records_one_removal(self) -> None:
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        collector, store = self.collector()
        self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
        self.run_pass(collector, UNREAD["core-show-unknown"](), {}, start + timedelta(minutes=5))
        empty, calls = self.run_pass(collector, KNOWN_EMPTY(), {}, start + timedelta(minutes=10))
        self.assertEqual(calls, 0)
        # A real read says the bay is empty: the removal and the cleared
        # evidence are both real now.
        self.assertIs(store.get_slot_states("s", "enc-a")[0].present, False)
        self.assertEqual(self.presence_changes(store), [(True, False)])
        self.assertEqual(empty["last_smart_failure_evidence_disks"], 0)
        self.assertIsNone(empty["last_max_temperature_celsius"])

    def test_unread_then_known_healthy_replaces_the_evidence(self) -> None:
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        collector, store = self.collector()
        self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
        self.run_pass(collector, UNREAD["quantastor-unknown"](), {}, start + timedelta(minutes=5))
        healthy, calls = self.run_pass(collector, KNOWN_PRESENT(), PASSED_COOL, start + timedelta(minutes=10))
        self.assertEqual(calls, 1)
        self.assertEqual(healthy["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(healthy["last_max_temperature_celsius"], 35)
        self.assertIs(store.get_slot_states("s", "enc-a")[0].present, True)
        self.assertEqual(self.presence_changes(store), [])

    def test_unread_bay_without_a_recorded_disk_stores_nothing(self) -> None:
        collector, store = self.collector()
        self.run_pass(collector, UNREAD["core-show-unknown"](), {}, datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(store.get_slot_states("s", "enc-a"), {})

    def test_known_empty_control_clears_without_an_unread_bay(self) -> None:
        # Control: a real empty read clears the evidence in the same pass shape.
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        collector, store = self.collector()
        self.run_pass(collector, KNOWN_PRESENT(), FAILED_HOT, start)
        empty, _calls = self.run_pass(collector, KNOWN_EMPTY(), {}, start + timedelta(minutes=5))
        self.assertEqual(empty["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(self.presence_changes(store), [(True, False)])


REPO_ROOT = Path(__file__).resolve().parents[1]
OCCUPANCY_FIELDS = {"present", "occupied", "presence_known"}
PRODUCTION_DIRS = ("app", "admin_service", "history_service")
# Whole files where these field names never mean a bay's occupancy.
OCCUPANCY_READ_EXEMPT_FILES = {
    "app/services/parsers.py": "SES parser records carry tri-state presence evidence before any slot view exists",
    "app/services/public_demo_fixture.py": "builds the synthetic public demo; no bay was read",
    "history_service/system_backup.py": "`present` is backup group membership, not a bay",
    "history_service/segment_catalog.py": "`present` is backup segment membership, not a bay",
    "admin_service/static/admin_backups.js": "`present` is a secret file on disk, not a bay",
}
# (file, function) -> (direct reads, reason). Nothing here states emptiness,
# counts a disk or words a message about the current occupant.
OCCUPANCY_READ_ALLOWLIST = {
    ("app/models/domain.py", "slot_occupancy"): (6, "the reader itself"),
    ("app/static/app.js", "slotOccupancy"): (4, "the page's reader itself"),
    ("admin_service/static/admin_backup_model.js", "secretStateText"): (2, "`present` is a secret file on disk, not a bay"),
    ("app/services/inventory.py", "InventoryService._build_slot_view"): (2, "producer: decides occupancy from raw evidence and sets presence_known"),
    ("app/services/inventory.py", "InventoryService._correlate_quantastor"): (1, "producer: raw SES candidate before the slot view"),
    ("app/services/inventory.py", "InventoryService._correlate_esxi_host"): (1, "producer: a whole StorCLI census decides raw empty evidence"),
    ("app/services/inventory.py", "InventoryService._merge_quantastor_ses_candidate"): (2, "producer: copies the SES candidate decision as one"),
    ("app/services/inventory.py", "InventoryService._slot_has_live_empty_evidence"): (2, "producer: raw hint dict feeding the empty gate"),
    ("app/services/inventory.py", "InventoryService._slot_identity_state"): (1, "identity key; unread groups with no-disk-known"),
    ("app/services/inventory.py", "InventoryService._slot_detail_entry_matches"): (1, "cache admission; an unread bay admits no cached disk"),
    ("app/services/inventory.py", "InventoryService._smart_disk_identity"): (1, "SMART cache key; an unread bay has no disk identity"),
    ("app/static/app.js", "candidateSlotsForSmartPrefetch"): (1, "SMART prefetch admission"),
    ("app/static/app.js", "ensureHeatmapData"): (1, "SMART prefetch admission for storage-view tiles"),
    ("app/static/app.js", "heatmapEntryOccupied"): (1, "heatmap scoring admission; declines an unread bay, draws no label"),
    ("app/static/app.js", "sendLedAction"): (1, "repairs an old saved state from raw evidence; raw unknown stays unknown"),
    ("app/static/app.js", "smartDiskIdentity"): (2, "SMART cache identity"),
    ("app/static/sas_fabric_view.js", "diskTraceHasDisk"): (1, "disk trace admission; an unread bay traces no disk"),
    ("app/static/sas_fabric_view.js", "smartCacheKey"): (1, "SMART cache key"),
    ("app/static/sas_fabric_view.js", "slotOccupancy"): (4, "the Storage Fabric page's reader itself, same rule as slotOccupancy in app.js"),
    ("app/static/sas_fabric_topology.js", "bayOccupancy"): (1, 'Topology reader of the fabric bay metric: only true or false is known; "unknown" and anything else draw unknown'),
    ("history_service/backup_7z.py", "_build_7z_archive_to_path"): (1, "`present` is backup group membership, not a bay"),
    ("history_service/collector.py", "HistoryCollector._has_stable_disk_identity"): (2, "compares two rows after unread bays took their recorded row"),
    ("history_service/collector.py", "HistoryCollector._scope_history_skip_reason"): (4, "degraded-scan guard: an unread new row skips the scope, keeping history"),
    ("history_service/domain.py", "build_slot_events"): (2, "compares two rows after unread bays took their recorded row"),
    ("history_service/domain.py", "summarize_record"): (1, "labels a stored row; unread bays write no row"),
}
_JS_READ = re.compile(r"(?:\?\.|\.)(present|occupied|presence_known)\b|\[\s*[\"'](present|occupied|presence_known)[\"']\s*\]")
_JS_FUNCTION = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(")


def _python_occupancy_reads(path: Path) -> list[str]:
    reads: list[str] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            field = None
            if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                field = child.attr
            elif isinstance(child, ast.Subscript) and isinstance(child.ctx, ast.Load) and isinstance(child.slice, ast.Constant):
                field = child.slice.value
            elif isinstance(child, ast.Call) and child.args:
                if isinstance(child.func, ast.Attribute) and child.func.attr == "get":
                    argument = child.args[0]
                elif isinstance(child.func, ast.Name) and child.func.id == "getattr" and len(child.args) > 1:
                    argument = child.args[1]
                else:
                    argument = None
                field = argument.value if isinstance(argument, ast.Constant) else None
            if field in OCCUPANCY_FIELDS:
                reads.append(".".join(scope) or "<module>")
            visit(child, scope)

    visit(ast.parse(path.read_text(encoding="utf-8")), [])
    return reads


def _javascript_occupancy_reads(path: Path) -> list[str]:
    reads: list[str] = []
    function = "<top>"
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := _JS_FUNCTION.match(line):
            function = match.group(1)
        if line.lstrip().startswith("//"):
            continue
        reads.extend(function for _match in _JS_READ.finditer(line))
    return reads


def production_occupancy_reads() -> dict[tuple[str, str], int]:
    """Direct reads of the occupancy fields in production code, per function."""

    counts: dict[tuple[str, str], int] = {}
    for directory in PRODUCTION_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in OCCUPANCY_READ_EXEMPT_FILES or path.suffix not in {".py", ".js"}:
                continue
            reads = _python_occupancy_reads(path) if path.suffix == ".py" else _javascript_occupancy_reads(path)
            for function in reads:
                counts[(relative, function)] = counts.get((relative, function), 0) + 1
    return counts


class OccupancyReaderTests(unittest.TestCase):
    def test_the_reader_says_present_empty_or_unknown(self) -> None:
        read = domain_models.slot_occupancy
        self.assertEqual(read(KNOWN_PRESENT()), "present")
        self.assertEqual(read(KNOWN_EMPTY()), "empty")
        for name, build in UNREAD.items():
            with self.subTest(name):
                view = build()
                self.assertEqual(read(view), "unknown")
                self.assertEqual(read(_payload(view)), "unknown")
                self.assertEqual(read(SlotStateRecord.from_snapshot_slot({"selected_system_id": "s"}, _payload(view))), "unknown")
        self.assertEqual(read(StorageViewRuntimeSlot(slot_index=0, slot_label="00", occupied=True)), "present")
        self.assertEqual(read(StorageViewRuntimeSlot(slot_index=0, slot_label="00")), "empty")
        self.assertEqual(read(StorageViewRuntimeSlot(slot_index=0, slot_label="00", presence_known=False)), "unknown")
        self.assertEqual(read({"occupied": False}), "empty")
        self.assertEqual(read({"present": True}), "present")
        self.assertEqual(read({"state": "healthy"}), "unknown")
        self.assertEqual(read(None), "unknown")

    def test_direct_occupancy_reads_stay_on_the_allowlist(self) -> None:
        found = production_occupancy_reads()
        expected = {key: count for key, (count, _reason) in OCCUPANCY_READ_ALLOWLIST.items()}
        self.assertEqual(
            {key: count for key, count in found.items() if expected.get(key) != count},
            {},
            "a production read of present/occupied/presence_known outside the reader: "
            "use slot_occupancy()/slotOccupancy(), or add it to the allowlist with a reason",
        )
        self.assertEqual({key for key in expected if key not in found}, set(), "stale allowlist entry")
        for key, (_count, reason) in OCCUPANCY_READ_ALLOWLIST.items():
            self.assertTrue(reason.strip(), key)
        for relative in OCCUPANCY_READ_EXEMPT_FILES:
            self.assertTrue((REPO_ROOT / relative).is_file(), relative)

    def test_the_scan_sees_a_new_direct_read(self) -> None:
        # Control for the scan itself: both parsers find the shapes they look for.
        with tempfile.TemporaryDirectory() as directory:
            python_file = Path(directory) / "probe.py"
            python_file.write_text(
                "def f(slot, payload):\n    return slot.present, payload.get('occupied'), payload['presence_known'], "
                "getattr(slot, 'present')\n",
                encoding="utf-8",
            )
            self.assertEqual(_python_occupancy_reads(python_file), ["f"] * 4)
            script = Path(directory) / "probe.js"
            script.write_text("  function g(slot) {\n    return slot?.occupied || slot.present;\n  }\n", encoding="utf-8")
            self.assertEqual(_javascript_occupancy_reads(script), ["g", "g"])

    def test_the_backup_model_allows_only_its_secret_state_reads(self) -> None:
        # Control for a function-scoped entry: the model file is scanned, and a new
        # function in it that reads a bay's presence is reported.
        relative = "admin_service/static/admin_backup_model.js"
        self.assertNotIn(relative, OCCUPANCY_READ_EXEMPT_FILES)
        expected = {key: count for key, (count, _reason) in OCCUPANCY_READ_ALLOWLIST.items()}
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "admin_backup_model.js"
            copy.write_text(source + "\nfunction syntheticBayRead(slot) {\n  return slot.present;\n}\n", encoding="utf-8")
            counts: dict[tuple[str, str], int] = {}
            for function in _javascript_occupancy_reads(copy):
                counts[(relative, function)] = counts.get((relative, function), 0) + 1
        self.assertEqual(counts, {(relative, "secretStateText"): 2, (relative, "syntheticBayRead"): 1})
        self.assertEqual(
            {key: count for key, count in counts.items() if expected.get(key) != count},
            {(relative, "syntheticBayRead"): 1},
        )


class FleetAlertEvidenceTests(unittest.TestCase):
    """SMART alert evidence is kept per disk, never as one fleet-wide block."""

    def run_passes(self, stages: list[dict[str, tuple[SlotView, dict]]]) -> list[dict[str, Any]]:
        # The real scope enumerator over a synthetic inventory transport.
        collector, _store = UnreadBayHistoryTests.collector(self)  # type: ignore[arg-type]
        stage: list[dict[str, tuple[SlotView, dict]]] = [stages[0]]

        async def fetch_inventory(system_id=None, enclosure_id=None, **_kwargs):
            enclosures = stage[0]
            selected = enclosure_id if enclosure_id in enclosures else next(iter(enclosures))
            view, _summaries = enclosures[selected]
            return {
                "selected_system_id": "s", "selected_enclosure_id": selected, "selected_system_platform": "core",
                "systems": [{"id": "s"}], "enclosures": [{"id": name} for name in enclosures],
                "slots": [{**_payload(view), "enclosure_id": selected}],
                "sources": {"api": {"enabled": True, "ok": True}},
            }

        async def smart(scope, _slots, **_kwargs):
            return stage[0][scope.enclosure_id][1]

        collector._fetch_inventory = fetch_inventory  # type: ignore[method-assign]
        collector._fetch_storage_views = AsyncMock(return_value={"views": []})  # type: ignore[method-assign]
        collector._fetch_smart_summaries = smart  # type: ignore[method-assign]
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        statuses = []
        for index, enclosures in enumerate(stages):
            stage[0] = enclosures
            with patch("history_service.collector.utcnow", return_value=start + timedelta(minutes=5 * index)):
                asyncio.run(collector.run_once(force_fast=True, include_due_intervals=False))
            scopes = asyncio.run(collector._enumerate_scopes(force_inventory=True))
            statuses.append({**collector.status(), "scopes": [scope.enclosure_id for scope in scopes]})
        return statuses

    @staticmethod
    def disk_b() -> SlotView:
        return KNOWN_PRESENT().model_copy(update={"serial": "SANITIZED-B", "gptid": "synthetic-id-b"})

    def test_removed_enclosure_evidence_clears_beside_an_unread_sibling(self) -> None:
        first = {"enc-a": (KNOWN_PRESENT(), FAILED_HOT), "enc-b": (self.disk_b(), PASSED_COOL)}
        later = {"enc-b": (UNREAD["core-show-unknown"](), {})}
        statuses = self.run_passes([first, later, later, later])
        self.assertEqual(statuses[0]["last_smart_failure_evidence_disks"], 1)
        self.assertEqual(statuses[0]["last_max_temperature_celsius"], 61)
        for status in statuses[1:]:
            self.assertEqual(status["scopes"], ["enc-b"])
            # A is in no configured scope: its failure and its 61 C are gone.
            self.assertEqual(status["last_smart_failure_evidence_disks"], 0)
            # B was not read again: it keeps only its own last reading.
            self.assertEqual(status["last_max_temperature_celsius"], 35)
            self.assertEqual(status["last_smart_failure_evidence_at"], statuses[0]["last_smart_failure_evidence_at"])

    def test_an_unread_bay_keeps_only_its_own_failure(self) -> None:
        # B failed and is now unread; A is read healthy every pass.
        first = {"enc-a": (KNOWN_PRESENT(), PASSED_COOL), "enc-b": (self.disk_b(), FAILED_HOT)}
        later = {"enc-a": (KNOWN_PRESENT(), PASSED_COOL), "enc-b": (UNREAD["quantastor-unknown"](), {})}
        recovered = {"enc-a": (KNOWN_PRESENT(), PASSED_COOL), "enc-b": (self.disk_b(), PASSED_COOL)}
        statuses = self.run_passes([first, later, later, recovered])
        for status in statuses[:3]:
            self.assertEqual(status["last_smart_failure_evidence_disks"], 1)
            self.assertEqual(status["last_max_temperature_celsius"], 61)
        # The evidence time is the oldest reading behind the total.
        self.assertEqual(statuses[2]["last_smart_failure_evidence_at"], statuses[0]["last_smart_failure_evidence_at"])
        # A real read of B replaces its evidence.
        self.assertEqual(statuses[3]["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(statuses[3]["last_max_temperature_celsius"], 35)
        self.assertGreater(statuses[3]["last_smart_failure_evidence_at"], statuses[0]["last_smart_failure_evidence_at"])

    def test_removed_enclosure_control_without_an_unread_bay(self) -> None:
        first = {"enc-a": (KNOWN_PRESENT(), FAILED_HOT), "enc-b": (self.disk_b(), PASSED_COOL)}
        later = {"enc-b": (self.disk_b(), PASSED_COOL)}
        statuses = self.run_passes([first, later])
        self.assertEqual(statuses[1]["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(statuses[1]["last_max_temperature_celsius"], 35)
        self.assertGreater(statuses[1]["last_smart_failure_evidence_at"], statuses[0]["last_smart_failure_evidence_at"])


def _snapshot(*slots: SlotView, rows: list[list[int | None]] | None = None) -> InventorySnapshot:
    return InventorySnapshot(
        slots=list(slots), layout_rows=rows or [[slot.slot for slot in slots]], layout_slot_count=len(slots),
        layout_columns=len(slots), refresh_interval_seconds=60, selected_system_id="s",
        selected_enclosure_id="enc-a", selected_system_platform="core",
    )


class UnreadBayConsumerTests(unittest.TestCase):
    def service(self):
        case = presence_cases.CoreSesutilShowPresenceTests()
        self.addCleanup(case.doCleanups)
        return case.service()

    def test_a_locate_light_on_an_unread_bay_keeps_occupancy_unknown(self) -> None:
        # The page's mapping-health test uses exactly this shape.
        view = _core_view("Slot 00  -  -  -  Identify")
        self.assertEqual((view.present, view.presence_known, view.state.value), (False, False, "identify"))
        self.assertEqual(domain_models.slot_occupancy(view), "unknown")

    def test_storage_fabric_says_unknown_for_an_unread_bay(self) -> None:
        service = self.service()
        for name, build, expected in [
            ("unread", UNREAD["core-show-unknown"], "unknown"),
            ("known-empty", KNOWN_EMPTY, False),
            ("known-present", KNOWN_PRESENT, True),
        ]:
            with self.subTest(name):
                fabric = build_sas_fabric_snapshot(system=service.system, snapshot=_snapshot(build()), ssh_outputs={})
                bays = [node for node in fabric.nodes if node.kind == "bay"]
                self.assertTrue(bays)
                for bay in bays:
                    self.assertEqual(bay.metrics["present"], expected)

    def test_saved_layout_bays_with_no_backing_row_are_unknown(self) -> None:
        service = self.service()
        view = StorageViewConfig(id="front", label="Front", kind="ses_enclosure", template_id="ses-auto")
        runtime = service._build_ses_storage_view_runtime(view, _snapshot(KNOWN_EMPTY()))
        backed = [slot for slot in runtime.slots if slot.slot_index == 0]
        missing = [slot for slot in runtime.slots if slot.slot_index != 0]
        self.assertEqual(len(missing), 23)
        # The real empty read stays empty.
        self.assertEqual([(s.occupied, s.presence_known, s.state) for s in backed], [(False, True, "empty")])
        for slot in missing:
            self.assertEqual((slot.occupied, slot.presence_known, slot.state), (False, False, "unknown"))
            self.assertEqual(domain_models.slot_occupancy(slot), "unknown")
            synthetic = service._build_slot_view_from_storage_view_runtime_slot(runtime, slot)
            self.assertEqual((synthetic.present, synthetic.presence_known, synthetic.state.value), (False, False, "unknown"))

    def test_manual_only_layout_bays_without_a_live_disk_are_unknown(self) -> None:
        live = KNOWN_PRESENT()
        slots = inventory_module._ManualOnlyStorageViewSlots(
            mappings={1: MAPPING.model_copy(update={"slot": 1, "serial": "SANITIZED-GONE"}), 2: MAPPING.model_copy(update={"slot": 2})},
            live_slots_by_key={key: live for key in inventory_module.normalize_lookup_keys(live.serial)},
        )
        no_assignment = slots.runtime_slot(0, "00", None)
        unresolved = slots.runtime_slot(1, "01", None)
        resolved = slots.runtime_slot(2, "02", None)
        self.assertEqual((no_assignment.state, domain_models.slot_occupancy(no_assignment)), ("unknown", "unknown"))
        self.assertEqual((unresolved.state, domain_models.slot_occupancy(unresolved)), ("unmapped", "unknown"))
        self.assertEqual((resolved.state, domain_models.slot_occupancy(resolved)), ("healthy", "present"))

    def test_inventory_binding_placeholders_stay_known_empty(self) -> None:
        # Control: an inventory-binding view lists every disk the platform
        # reports, so a bay no disk lands in is a real empty bay.
        service = self.service()
        view = StorageViewConfig(id="nvme", label="NVMe", kind="nvme_carrier", template_id="nvme-carrier-4")
        runtime = service._build_candidate_storage_view_runtime(view, _snapshot(KNOWN_EMPTY()), [], set())
        self.assertTrue(runtime.slots)
        for slot in runtime.slots:
            self.assertEqual((domain_models.slot_occupancy(slot), slot.state), ("empty", "empty"))

    def test_retention_accounting_does_not_count_an_unread_saved_assignment(self) -> None:
        unread = UNREAD["quantastor-unresolved-mapping"]()
        self.assertEqual(unread.serial, "SANITIZED-MAPPED")
        retention = build_disk_retention_accounting(source_disks=[], slots=[unread])
        self.assertEqual((retention.source_disk_count, retention.rendered_unique_disk_count), (0, 0))
        # Control: a bay read as holding the disk is a rendered disk.
        present = build_disk_retention_accounting(source_disks=[], slots=[KNOWN_PRESENT()])
        self.assertEqual(present.rendered_unique_disk_count, 1)

    def test_restore_preview_marks_an_unread_bay_as_not_checkable(self) -> None:
        warn = inventory_module.InventoryService._mapping_import_disk_warning
        backup = ManualMapping(slot=0, serial="SANITIZED-BACKUP")
        unread = warn(("enc-a", 0), backup, UNREAD["quantastor-unresolved-mapping"]())
        self.assertEqual((unread["current"], unread["occupancy"]), (None, "unknown"))
        # Controls keep the shape they had: no occupancy key when it was read.
        empty = warn(("enc-a", 0), backup, KNOWN_EMPTY())
        self.assertEqual((empty["current"], "occupancy" in empty), (None, False))
        present = warn(("enc-a", 0), backup, KNOWN_PRESENT())
        self.assertEqual((present["current"], "occupancy" in present), ("SANITIZED-MAPPED", False))



TEMPERATURE_ONLY = {0: {"available": True, "temperature_c": 35}}
HEALTH_ONLY = {0: {"available": True, "smart_health_status": "FAILED"}}
PASSED_WARM = {0: {"available": True, "smart_health_status": "PASSED", "temperature_c": 40}}


class PartialSmartSummaryThenUnreadTests(unittest.TestCase):
    """A SMART summary may lack optional fields; the next unread pass still completes."""

    def run_passes(self, stages: list[tuple[SlotView, dict]]) -> list[dict[str, Any]]:
        collector, store = UnreadBayHistoryTests.collector(self)  # type: ignore[arg-type]
        retention = MagicMock(wraps=collector._run_retention_if_due)
        collector._run_retention_if_due = retention  # type: ignore[method-assign]
        start = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        statuses = []
        for index, (view, summaries) in enumerate(stages):
            at = start + timedelta(minutes=5 * index)
            collector._enumerate_scopes = AsyncMock(return_value=[ScopeSnapshot(  # type: ignore[method-assign]
                system_id="s", system_label="S", enclosure_id="enc-a", enclosure_label="Shelf",
                snapshot={"selected_system_id": "s", "selected_enclosure_id": "enc-a", "slots": [_payload(view)]},
            )])
            collector._fetch_smart_summaries = AsyncMock(return_value=summaries)  # type: ignore[method-assign]
            with patch("history_service.collector.utcnow", return_value=at):
                try:
                    asyncio.run(collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))
                except Exception as exc:  # noqa: BLE001 - the assertion names the failure.
                    self.fail(f"collection pass {index} raised {exc!r}")
            status = collector.status()
            # The pass ran to the end: backup, retention and the success time.
            self.assertEqual(status["last_success_at"], at.isoformat(), index)
            self.assertEqual(store.create_backup.call_count, index + 1, index)  # type: ignore[attr-defined]
            self.assertEqual(retention.call_count, index + 1, index)
            statuses.append(status)
        return statuses

    def test_temperature_only_then_unread_bays_complete_and_keep_the_temperature(self) -> None:
        unread = UNREAD["core-show-unknown"]
        statuses = self.run_passes([
            (KNOWN_PRESENT(), TEMPERATURE_ONLY), (unread(), {}), (unread(), {}),
            (KNOWN_PRESENT(), PASSED_WARM), (KNOWN_EMPTY(), {}),
        ])
        first, *unread_passes, healthy, empty = statuses
        for status in unread_passes:
            # The disk was not read again: its own temperature and its time stand.
            self.assertEqual(status["last_max_temperature_celsius"], 35)
            self.assertEqual(status["last_temperature_evidence_at"], first["last_temperature_evidence_at"])
            self.assertEqual(status["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(healthy["last_max_temperature_celsius"], 40)
        self.assertGreater(healthy["last_temperature_evidence_at"], first["last_temperature_evidence_at"])
        self.assertEqual(healthy["last_smart_failure_evidence_disks"], 0)
        self.assertIsNone(empty["last_max_temperature_celsius"])
        self.assertEqual(empty["last_smart_failure_evidence_disks"], 0)

    def test_health_only_then_unread_bays_keep_the_failure(self) -> None:
        # Control: a summary with health and no temperature.
        unread = UNREAD["quantastor-unknown"]
        statuses = self.run_passes([
            (KNOWN_PRESENT(), HEALTH_ONLY), (unread(), {}), (unread(), {}), (KNOWN_PRESENT(), PASSED_WARM),
        ])
        first, *unread_passes, healthy = statuses
        self.assertEqual(first["last_smart_failure_evidence_disks"], 1)
        for status in unread_passes:
            self.assertEqual(status["last_smart_failure_evidence_disks"], 1)
            self.assertEqual(status["last_smart_failure_evidence_at"], first["last_smart_failure_evidence_at"])
        self.assertEqual(healthy["last_smart_failure_evidence_disks"], 0)
        self.assertEqual(healthy["last_max_temperature_celsius"], 40)


class ManualOnlyUnreadIdentityTests(unittest.TestCase):
    """A manual-only view takes a saved serial as a live disk only from a bay read as holding it."""

    def runtime_bay(self, live: SlotView) -> tuple[StorageViewRuntimeSlot, int]:
        from app.services.profile_registry import UNIFI_LAYOUT_ONLY_PROFILE_IDS

        case = presence_cases.CoreSesutilShowPresenceTests()
        self.addCleanup(case.doCleanups)
        service = case.service()
        profile = service.profile_registry.get("supermicro-ssg-2028r-shared-front-24")
        snapshot = InventorySnapshot(
            slots=[live], selected_system_id=service.system.id, selected_enclosure_id="enc-a",
            selected_profile=profile, refresh_interval_seconds=60,
        )
        service.mapping_store.save_mapping(ManualMapping(
            system_id=service.system.id, enclosure_id="enc-a", slot=2, serial="SANITIZED-MAPPED",
        ))
        view = StorageViewConfig(
            id="manual-pinned", label="Manual pinned", kind="ses_enclosure", template_id="ses-auto",
            profile_id=sorted(UNIFI_LAYOUT_ONLY_PROFILE_IDS)[0],
        )
        runtime = service._build_ses_storage_view_runtime(view, snapshot)
        return next(slot for slot in runtime.slots if slot.slot_index == 2), runtime.matched_count

    def test_an_unread_bay_with_the_saved_serial_is_not_a_live_disk(self) -> None:
        unread = UNREAD["quantastor-unresolved-mapping"]()
        self.assertEqual((unread.serial, domain_models.slot_occupancy(unread)), ("SANITIZED-MAPPED", "unknown"))
        bay, matched = self.runtime_bay(unread)
        self.assertEqual(
            (bay.occupied, bay.presence_known, bay.state, domain_models.slot_occupancy(bay), matched),
            (False, False, "unmapped", "unknown", 0),
        )
        # It stays a saved assignment that cannot be verified.
        self.assertEqual(bay.serial, "SANITIZED-MAPPED")

    def test_a_bay_read_as_holding_the_disk_still_resolves(self) -> None:
        bay, matched = self.runtime_bay(KNOWN_PRESENT())
        self.assertEqual(
            (bay.occupied, bay.state, domain_models.slot_occupancy(bay), matched), (True, "healthy", "present", 1),
        )

    def test_a_confirmed_empty_bay_is_not_a_live_disk(self) -> None:
        for name, live in {
            "no identity": KNOWN_EMPTY(),
            # An empty row that still names the saved serial is no live disk either.
            "saved serial": KNOWN_EMPTY().model_copy(update={"serial": "SANITIZED-MAPPED"}),
        }.items():
            with self.subTest(name):
                self.assertEqual(domain_models.slot_occupancy(live), "empty")
                bay, matched = self.runtime_bay(live)
                self.assertEqual(
                    (bay.occupied, bay.state, domain_models.slot_occupancy(bay), matched),
                    (False, "unmapped", "unknown", 0),
                )


UNVERIFIABLE_SAVE = "The disk in this bay could not be checked right now. Nothing was saved. Reload and try again."
UNVERIFIABLE_CLEAR = "The disk in this bay could not be checked right now. Nothing was cleared. Reload and try again."
CHANGED_SAVE = "The disk in this bay changed. Reload to see it, then save again."
CHANGED_CLEAR = "The disk in this bay changed. Reload to see it, then clear again."


class MappingWriteAgainstAnUnreadBayTests(unittest.TestCase):
    """A save or clear is refused when the pre-write read could not tell what is in the bay."""

    def write(self, action: str, editor_saw: SlotView, now: SlotView) -> tuple[Any, Any]:
        from app.services.mapping_store import bind_mapping_revision

        case = presence_cases.CoreSesutilShowPresenceTests()
        self.addCleanup(case.doCleanups)
        service = case.service()
        service._get_pre_write_snapshot = AsyncMock(  # type: ignore[method-assign]
            return_value=InventorySnapshot(
                slots=[now.model_copy(update={"enclosure_id": "enc-a"})], refresh_interval_seconds=60,
            ),
        )
        store = service.mapping_store
        system_id = service.system.id
        if action == "clear":
            store.save_mapping(ManualMapping(system_id=system_id, enclosure_id="enc-a", slot=0, serial="SANITIZED-MAPPED"))
            revision = store.clear_revision(system_id, "enc-a", 0)
        else:
            revision = store.save_revision(system_id, "enc-a", 0)
        # The revision the editor got, bound to the bay as it showed it.
        expected = bind_mapping_revision(
            revision, service._slot_disk_binding(editor_saw.model_copy(update={"enclosure_id": "enc-a"})),
        )
        try:
            if action == "clear":
                asyncio.run(service.clear_mapping(0, "enc-a", expected_revision=expected, invalidate_snapshot=False))
            else:
                asyncio.run(service.save_mapping(
                    0, {"serial": "SANITIZED-NEW"}, "enc-a", expected_revision=expected, invalidate_snapshot=False,
                ))
            error = None
        except Exception as exc:  # noqa: BLE001 - the assertions name the refusal.
            error = exc
        stored = store.get_mapping(system_id, "enc-a", 0)
        return error, stored.serial if stored else None

    def test_an_unread_pre_write_read_is_refused_as_not_checkable(self) -> None:
        unread = UNREAD["core-show-unknown"]
        for action, text, kept in (("save", UNVERIFIABLE_SAVE, None), ("clear", UNVERIFIABLE_CLEAR, "SANITIZED-MAPPED")):
            # The editor showed a disk, an empty bay, or the same unread bay.
            for name, editor_saw in {"disk": KNOWN_PRESENT, "empty": KNOWN_EMPTY, "unread": unread}.items():
                with self.subTest(action=action, editor_saw=name):
                    error, stored = self.write(action, editor_saw(), unread())
                    self.assertIsNotNone(error, "an unread bay let the write through")
                    self.assertEqual(
                        (getattr(error, "error_code", None), getattr(error, "public_detail", None), str(error)),
                        ("mapping_disk_unverifiable", text, text),
                    )
                    self.assertTrue(getattr(error, "current_revision", None))
                    # Nothing was written.
                    self.assertEqual(stored, kept)

    def test_a_verified_change_keeps_the_disk_changed_refusal(self) -> None:
        other_disk = lambda: KNOWN_PRESENT().model_copy(update={"serial": "SANITIZED-OTHER"})  # noqa: E731
        for action, text in (("save", CHANGED_SAVE), ("clear", CHANGED_CLEAR)):
            for name, now in {"other disk": other_disk, "empty bay": KNOWN_EMPTY}.items():
                with self.subTest(action=action, now=name):
                    error, _stored = self.write(action, KNOWN_PRESENT(), now())
                    self.assertEqual((getattr(error, "error_code", None), str(error)), ("mapping_disk_changed", text))

    def test_the_same_disk_still_saves_and_clears(self) -> None:
        self.assertEqual(self.write("save", KNOWN_PRESENT(), KNOWN_PRESENT()), (None, "SANITIZED-NEW"))
        self.assertEqual(self.write("clear", KNOWN_PRESENT(), KNOWN_PRESENT()), (None, None))


STORCLI_LIST = "/opt/lsi/storcli64/storcli64 /c{controller}/eall/sall show all J"


def _storcli_reply(status: str = "Success", slot_text: str = "13:0") -> str:
    drive = {"Ctl": "c0", "EID:Slt": slot_text, "DID": 0, "State": "Onln", "DG": "0", "Size": "1 TB",
             "Intf": "NVMe", "Med": "SSD", "SeSz": "512B", "Model": "Synthetic", "SN": "SANITIZED-M2"}
    return json.dumps({"Controllers": [{
        "Command Status": {"Controller": 0, "Status": status, "Description": "None"},
        "Response Data": {"Drive Information": [drive]},
    }]})


class StorcliOnlyEnumerationTests(unittest.TestCase):
    """A complete StorCLI drive list on the RAID-card layout leaves the bays it did not list empty."""

    def correlate(self, outputs: dict[str, str], *, profile_id: str | None = None, controllers: tuple[int, ...] = (0,)):
        from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
        from app.services.mapping_store import MappingStore
        from app.services.parsers import ParsedSSHData, parse_ssh_outputs
        from app.services.profile_registry import ESXI_AOC_SLG4_2H8M2_PROFILE_ID, ProfileRegistry
        from app.services.slot_detail_store import SlotDetailStore
        from app.services.truenas_ws import TrueNASRawData

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        system = SystemConfig(
            id="synthetic-esxi", truenas=TrueNASConfig(platform="esxi"),
            default_profile_id=profile_id or ESXI_AOC_SLG4_2H8M2_PROFILE_ID,
            ssh=SSHConfig(enabled=False, commands=[STORCLI_LIST.format(controller=c) for c in controllers]),
        )
        service = inventory_module.InventoryService(
            settings, system, AsyncMock(), AsyncMock(), None, MappingStore(str(Path(directory.name) / "m.json")),
            ProfileRegistry(settings), SlotDetailStore(str(Path(directory.name) / "d.json")),
        )
        raw = TrueNASRawData(enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[])
        slots, *_rest = service._correlate(
            raw, parse_ssh_outputs(outputs, 2, None), [], quantastor_ses_data=ParsedSSHData(),
        )
        return service, slots

    def test_a_complete_list_leaves_an_unlisted_bay_empty(self) -> None:
        service, slots = self.correlate({STORCLI_LIST.format(controller=0): _storcli_reply()})
        by_slot = {slot.slot: slot for slot in slots}
        self.assertEqual(
            (domain_models.slot_occupancy(by_slot[0]), by_slot[0].state.value, by_slot[0].serial),
            ("present", "healthy", "SANITIZED-M2"),
        )
        self.assertEqual((domain_models.slot_occupancy(by_slot[1]), by_slot[1].state.value), ("empty", "empty"))
        fabric = build_sas_fabric_snapshot(system=service.system, snapshot=_snapshot(*slots), ssh_outputs={})
        presence = sorted(str(node.metrics["present"]) for node in fabric.nodes if node.kind == "bay")
        self.assertEqual(presence, ["False", "True"])

    def test_a_failed_partial_or_unsupported_list_leaves_the_bay_unknown(self) -> None:
        listing = STORCLI_LIST.format(controller=0)
        cases = {
            "command failed": ({listing: _storcli_reply(status="Failure")}, {}),
            "a row without a slot": ({listing: _storcli_reply(slot_text="13:x")}, {}),
            "not json": ({listing: "Controller = 0\nStatus = Success"}, {}),
            "a second listing did not answer": ({listing: _storcli_reply()}, {"controllers": (0, 1)}),
            "no listing": ({}, {}),
            "another layout": ({listing: _storcli_reply()}, {"profile_id": "supermicro-ssg-2028r-shared-front-24"}),
        }
        for name, (outputs, options) in cases.items():
            with self.subTest(name):
                _service, slots = self.correlate(outputs, **options)
                unlisted = [slot for slot in slots if slot.serial != "SANITIZED-M2"]
                self.assertTrue(unlisted)
                for slot in unlisted:
                    self.assertEqual(domain_models.slot_occupancy(slot), "unknown", slot.slot)


def _storcli_drive(slot: int, controller: int = 0, slot_text: str | None = None) -> dict[str, Any]:
    return {"Ctl": f"c{controller}", "EID:Slt": slot_text or f"13:{slot}", "DID": slot, "State": "Onln", "DG": "0",
            "Size": "1 TB", "Intf": "NVMe", "Med": "SSD", "SeSz": "512B", "Model": "Synthetic",
            "SN": f"SANITIZED-C{controller}-S{slot}"}


def _storcli_block(drives: list[Any], *, controller: int = 0, form: str = "table",
                   status: str = "Success") -> dict[str, Any]:
    """One controller block of an `/eall/sall show all J` reply, in either form the drive parser reads."""
    if form == "table":
        response: dict[str, Any] = {"Drive Information": drives}
    else:
        # The per-drive form: one `Drive /cN/eM/sK` entry per drive, beside its detailed entry.
        response = {}
        for drive in drives:
            path = f"Drive /c{controller}/e13/s{drive['DID']}"
            response[path] = [drive]
            response[f"{path} - Detailed Information"] = {
                f"{path} Device attributes": {"SN": drive["SN"]},
                f"{path} Port Information": {"Connector Name": f"C{drive['DID']} x4"},
            }
    return {"Command Status": {"Controller": controller, "Status": status, "Description": "None"},
            "Response Data": response}


def _storcli_drives_reply(*blocks: dict[str, Any]) -> str:
    return json.dumps({"Controllers": list(blocks)})


class StorcliPerDriveCensusTests(unittest.TestCase):
    """A successful reply in the per-drive form is a whole census, as the table form is.

    Through the real SSH parser, the ESXi correlation, the SES storage-view
    runtime and the Storage Fabric builder.
    """

    def read(self, outputs: dict[str, str], *, controllers: tuple[int, ...] = (0,)) -> dict[str, Any]:
        from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
        from app.services.mapping_store import MappingStore
        from app.services.parsers import parse_ssh_outputs
        from app.services.profile_registry import ESXI_AOC_SLG4_2H8M2_PROFILE_ID, ProfileRegistry
        from app.services.slot_detail_store import SlotDetailStore

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        system = SystemConfig(
            id="synthetic-esxi", truenas=TrueNASConfig(platform="esxi"),
            default_profile_id=ESXI_AOC_SLG4_2H8M2_PROFILE_ID,
            ssh=SSHConfig(enabled=False, commands=[STORCLI_LIST.format(controller=c) for c in controllers]),
        )
        service = inventory_module.InventoryService(
            settings, system, AsyncMock(), AsyncMock(), None, MappingStore(str(Path(directory.name) / "m.json")),
            ProfileRegistry(settings), SlotDetailStore(str(Path(directory.name) / "d.json")),
        )
        parsed = parse_ssh_outputs(outputs, 2, None)
        slots, _enclosures, meta, rows, count, columns = service._correlate(None, parsed, [])
        snapshot = InventorySnapshot(
            slots=slots, selected_system_id=system.id, selected_enclosure_id=meta.get("id"),
            selected_profile=service.profile_registry.get(ESXI_AOC_SLG4_2H8M2_PROFILE_ID),
            layout_rows=rows, layout_slot_count=count, layout_columns=columns, refresh_interval_seconds=60,
        )
        runtime = service._build_ses_storage_view_runtime(
            StorageViewConfig(id="physical", label="Physical", kind="ses_enclosure", template_id="ses-auto"), snapshot,
        )
        fabric = build_sas_fabric_snapshot(system=system, snapshot=snapshot, ssh_outputs={})
        by_slot = {slot.slot: slot for slot in slots}
        return {
            "census": dict(parsed.esxi_storcli_drive_lists),
            "bays": [(slot, domain_models.slot_occupancy(by_slot[slot]), by_slot[slot].serial) for slot in (0, 1)],
            "runtime": sorted((item.slot_index, item.occupied, item.presence_known) for item in runtime.slots),
            "fabric": [node.metrics["present"] for node in sorted(
                (node for node in fabric.nodes if node.kind == "bay"), key=lambda node: node.slot)],
        }

    def test_a_per_drive_reply_with_one_drive_leaves_the_other_bay_empty(self) -> None:
        listing = STORCLI_LIST.format(controller=0)
        result = self.read({listing: _storcli_drives_reply(_storcli_block([_storcli_drive(0)], form="per-drive"))})
        self.assertEqual(result["census"], {"storcli /c0/eall/sall show all J": True})
        self.assertEqual(result["bays"], [(0, "present", "SANITIZED-C0-S0"), (1, "empty", None)])
        # The storage view shows the bay as read and empty (`Empty`), not unknown.
        self.assertEqual(result["runtime"], [(0, True, True), (1, False, True)])
        self.assertEqual(result["fabric"], [True, False])

    def test_a_per_drive_reply_with_two_drives_keeps_both(self) -> None:
        listing = STORCLI_LIST.format(controller=0)
        result = self.read({listing: _storcli_drives_reply(
            _storcli_block([_storcli_drive(0), _storcli_drive(1)], form="per-drive"),
        )})
        self.assertEqual(result["census"], {"storcli /c0/eall/sall show all J": True})
        self.assertEqual(result["bays"], [(0, "present", "SANITIZED-C0-S0"), (1, "present", "SANITIZED-C0-S1")])
        self.assertEqual(result["fabric"], [True, True])

    def test_the_table_form_is_unchanged(self) -> None:
        listing = STORCLI_LIST.format(controller=0)
        result = self.read({listing: _storcli_drives_reply(_storcli_block([_storcli_drive(0)]))})
        self.assertEqual(result["census"], {"storcli /c0/eall/sall show all J": True})
        self.assertEqual(result["bays"], [(0, "present", "SANITIZED-C0-S0"), (1, "empty", None)])
        self.assertEqual(result["runtime"], [(0, True, True), (1, False, True)])
        self.assertEqual(result["fabric"], [True, False])

    def test_two_controllers_one_answering_in_each_form_are_both_a_census(self) -> None:
        first, second = STORCLI_LIST.format(controller=0), STORCLI_LIST.format(controller=1)
        result = self.read({
            first: _storcli_drives_reply(_storcli_block([_storcli_drive(0)])),
            second: _storcli_drives_reply(_storcli_block([_storcli_drive(1, controller=1)], controller=1, form="per-drive")),
        }, controllers=(0, 1))
        self.assertEqual(result["census"], {"storcli /c0/eall/sall show all J": True,
                                            "storcli /c1/eall/sall show all J": True})
        self.assertEqual(result["bays"], [(0, "present", "SANITIZED-C0-S0"), (1, "present", "SANITIZED-C1-S1")])
        self.assertEqual(result["fabric"], [True, True])
        # The same two forms in one `/call` reply.
        listing = "/opt/lsi/storcli64/storcli64 /call/eall/sall show all J"
        result = self.read({listing: _storcli_drives_reply(
            _storcli_block([_storcli_drive(0)]),
            _storcli_block([_storcli_drive(1, controller=1)], controller=1, form="per-drive"),
        )}, controllers=())
        self.assertEqual(result["census"], {"storcli /call/eall/sall show all J": True})
        self.assertEqual(result["bays"], [(0, "present", "SANITIZED-C0-S0"), (1, "present", "SANITIZED-C1-S1")])

    def test_a_failed_partial_or_contradicting_reply_leaves_the_unlisted_bay_unknown(self) -> None:
        listing = STORCLI_LIST.format(controller=0)
        one = [_storcli_drive(0)]
        # A table that names bay 0 and a per-drive entry for bay 1 in the same reply.
        contradicting = _storcli_block(one)
        contradicting["Response Data"].update(_storcli_block([_storcli_drive(1)], form="per-drive")["Response Data"])
        # A per-drive entry whose row names another slot than its entry.
        mislabelled = _storcli_block(one, form="per-drive")
        mislabelled["Response Data"]["Drive /c0/e13/s1"] = mislabelled["Response Data"].pop("Drive /c0/e13/s0")
        cases = {
            "failed status": ({listing: _storcli_drives_reply(_storcli_block(one, form="per-drive", status="Failure"))},
                              (0,)),
            "a second controller block failed": ({listing: _storcli_drives_reply(
                _storcli_block(one, form="per-drive"), _storcli_block([], controller=1, status="Failure"),
            )}, (0,)),
            "a configured listing did not answer": ({listing: _storcli_drives_reply(
                _storcli_block(one, form="per-drive"))}, (0, 1)),
            "an unparseable slot": ({listing: _storcli_drives_reply(_storcli_block(
                [_storcli_drive(0), _storcli_drive(1, slot_text="13:x")], form="per-drive",
            ))}, (0,)),
            "a table row that is not an object": ({listing: _storcli_drives_reply(
                _storcli_block([_storcli_drive(0), "13:1 garbled"]))}, (0,)),
            "a table and a per-drive entry name different drives": (
                {listing: _storcli_drives_reply(contradicting)}, (0,)),
            "a per-drive entry names another slot than its row": (
                {listing: _storcli_drives_reply(mislabelled)}, (0,)),
        }
        for name, (outputs, controllers) in cases.items():
            with self.subTest(name):
                result = self.read(outputs, controllers=controllers)
                self.assertEqual(result["bays"][0], (0, "present", "SANITIZED-C0-S0"))
                self.assertEqual(result["bays"][1][1], "unknown")
                self.assertEqual(result["runtime"][1], (1, False, False))
                self.assertEqual(result["fabric"][1], "unknown")
                # Either a listing was not a census, or a configured one did not answer.
                self.assertTrue(False in result["census"].values() or len(result["census"]) < len(controllers))
        # A `Success` reply that lists no drive at all is not a census either.
        no_entries = _storcli_block([])
        no_entries["Response Data"] = {}
        for name, block in {"an empty table": _storcli_block([]), "no drive entries": no_entries}.items():
            with self.subTest(name):
                result = self.read({listing: _storcli_drives_reply(block)})
                self.assertEqual(result["census"], {"storcli /c0/eall/sall show all J": False})
                self.assertEqual([bay[1] for bay in result["bays"]], ["unknown", "unknown"])
                self.assertEqual(result["fabric"], ["unknown", "unknown"])
        # Control: the same per-drive reply alone is a census (above), and a
        # table that names the same drives as its per-drive entries is too.
        agreeing = _storcli_block(one)
        agreeing["Response Data"].update(_storcli_block(one, form="per-drive")["Response Data"])
        result = self.read({listing: _storcli_drives_reply(agreeing)})
        self.assertEqual(result["bays"][1][1], "empty")
        self.assertEqual(result["fabric"], [True, False])


class RestoreConfirmationAfterAReadChangeTests(unittest.TestCase):
    """A restore previewed on an empty bay and confirmed on an unread one (or the reverse) is refused."""

    BAYS_CHANGED = "Bays changed since the preview. Preview the import again."

    def restore(self, at_preview: SlotView, at_confirmation: SlotView) -> tuple[Any, Any]:
        from app.models.domain import MappingBundle

        case = presence_cases.CoreSesutilShowPresenceTests()
        self.addCleanup(case.doCleanups)
        service = case.service()
        reads = iter((at_preview, at_confirmation))
        service._get_pre_write_snapshot = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda *_args, **_kwargs: InventorySnapshot(
                slots=[next(reads).model_copy(update={"enclosure_id": "enc-a"})], refresh_interval_seconds=60,
            ),
        )
        bundle = MappingBundle(mappings=[ManualMapping(
            system_id=service.system.id, enclosure_id="enc-a", slot=0, serial="SANITIZED-RESTORED",
        )])
        preview = asyncio.run(service.preview_mapping_bundle(bundle, "enc-a"))
        try:
            asyncio.run(service.import_mapping_bundle(
                bundle, "enc-a", expected_revision=preview["revision"], import_digest=preview["import_digest"],
                invalidate_snapshot=False,
            ))
            error = None
        except Exception as exc:  # noqa: BLE001 - the assertions name the refusal.
            error = exc
        stored = service.mapping_store.get_mapping(service.system.id, "enc-a", 0)
        return error, stored.serial if stored else None

    def test_empty_then_unread_and_unread_then_empty_are_refused(self) -> None:
        unread = UNREAD["core-show-unknown"]
        for name, (at_preview, at_confirmation) in {
            "empty at preview, unread at confirmation": (KNOWN_EMPTY, unread),
            "unread at preview, empty at confirmation": (unread, KNOWN_EMPTY),
        }.items():
            with self.subTest(name):
                error, stored = self.restore(at_preview(), at_confirmation())
                self.assertEqual(
                    (getattr(error, "error_code", None), getattr(error, "public_detail", None)),
                    ("mapping_import_bays_changed", self.BAYS_CHANGED),
                )
                self.assertIsNone(stored)

    def test_an_unchanged_read_restores(self) -> None:
        unread = UNREAD["core-show-unknown"]
        for name, read in {"empty both times": KNOWN_EMPTY, "unread both times": unread}.items():
            with self.subTest(name):
                self.assertEqual(self.restore(read(), read()), (None, "SANITIZED-RESTORED"))


if __name__ == "__main__":
    unittest.main()
