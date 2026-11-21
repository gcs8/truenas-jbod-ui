"""Mapping export, import preview and the import's pre-check read run off the event loop.

A slow mapping-file read must not stall other requests on the same loop. The
read runs on a worker thread that the request keeps until it finishes, even
through cancellation, and readers still never wait for a writer lock. The
result and the errors the read raises are unchanged. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.config import SystemConfig
from app.models.domain import InventorySnapshot, ManualMapping, MappingBundle
from app.services import mapping_store as mapping_store_module
from app.services.inventory import InventoryService
from app.services.mapping_store import (
    MappingReadBusy,
    MappingRevisionConflict,
    MappingScopeConflict,
    MappingStore,
)

# A loop that runs the read itself cannot release it; only this outside timer
# can, long after a read on a worker thread was released by the loop's timer.
RESCUE_SECONDS = 1.0


def mapping(slot: int, notes: str) -> ManualMapping:
    return ManualMapping(system_id="sys-a", enclosure_id="shelf-a", slot=slot, notes=notes)


def seeded_store(directory: str) -> tuple[Path, MappingStore]:
    path = Path(directory) / "slot_mappings.json"
    store = MappingStore(path)
    store.save_mapping(mapping(1, "first"))
    store.save_mapping(mapping(2, "second"))
    return path, store


def bare_service(store: MappingStore) -> InventoryService:
    """The real async mapping methods; appliance reads replaced (an empty bundle reads none)."""
    service = object.__new__(InventoryService)
    service.system = SystemConfig(id="sys-a")
    service.mapping_store = store
    service._get_pre_write_snapshot = AsyncMock(
        return_value=InventorySnapshot(
            slots=[], layout_rows=[], layout_slot_count=0, layout_columns=1, refresh_interval_seconds=30,
        )
    )
    service._read_mapping_import_bays = AsyncMock(return_value={})
    service.invalidate_physical_enclosure_snapshot_cache = Mock()
    service.invalidate_snapshot_cache = Mock()
    return service


class _GatedLstat:
    """Hold the mapping file's ``lstat`` until released; record the reading thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []
        self._original = os.lstat

    def __call__(self, name, *args, **kwargs):
        if Path(name) == self.path and not self.release.is_set():
            self.threads.append(threading.current_thread())
            self.entered.set()
            self.release.wait(timeout=10)
        return self._original(name, *args, **kwargs)

    def patch(self):
        return patch.object(mapping_store_module.os, "lstat", self)


def operations(service: InventoryService, revision: str):
    bundle = MappingBundle(mappings=[])
    return {
        "export": lambda: service.export_mapping_bundle(selected_enclosure_id="shelf-a"),
        "preview": lambda: service.preview_mapping_bundle(bundle, selected_enclosure_id="shelf-a"),
        # A stale confirmation is refused by the pre-check read, before any write.
        "import_precheck": lambda: service.import_mapping_bundle(
            bundle, selected_enclosure_id="shelf-a", expected_revision="0" * len(revision),
            import_digest="0" * 64,
        ),
    }


class MappingReadsLeaveTheLoopFreeTests(unittest.TestCase):
    def run_with_heartbeat(self, gate: _GatedLstat, read):
        """Run ``read`` while another request's 20 ms timer is due; it releases the read."""
        loop_threads: list[threading.Thread] = []

        async def scenario():
            loop = asyncio.get_running_loop()
            loop_threads.append(threading.current_thread())
            started = loop.time()
            ticks: list[float] = []

            def other_request() -> None:
                ticks.append(loop.time() - started)
                gate.release.set()

            loop.call_later(0.02, other_request)
            try:
                result = await read()
            except Exception as exc:  # the error mapping is checked by the caller
                result = exc
            return ticks, result

        rescue = threading.Timer(RESCUE_SECONDS, gate.release.set)
        rescue.start()
        try:
            with gate.patch():
                ticks, result = asyncio.run(scenario())
        finally:
            gate.release.set()
            rescue.cancel()
            rescue.join()
        return ticks, result, loop_threads[0]

    def test_a_slow_mapping_read_does_not_block_another_request_on_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            revision = store.scope_revision("sys-a", "shelf-a")
            for name, read in operations(bare_service(MappingStore(path)), revision).items():
                with self.subTest(operation=name):
                    gate = _GatedLstat(path)
                    ticks, result, loop_thread = self.run_with_heartbeat(gate, read)
                    self.assertTrue(gate.threads, "the operation never read the mapping file")
                    self.assertNotIn(
                        loop_thread, gate.threads, f"{name} read the mapping file on the event loop thread",
                    )
                    self.assertTrue(ticks, "the other request's timer never ran")
                    self.assertLess(
                        ticks[0], RESCUE_SECONDS / 2,
                        f"{name} blocked the event loop until the outside rescue released its read",
                    )
                    if name == "import_precheck":
                        self.assertIsInstance(result, MappingRevisionConflict)
                    else:
                        self.assertNotIsInstance(result, Exception)

    def test_a_cancelled_request_keeps_its_read_until_the_worker_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            revision = store.scope_revision("sys-a", "shelf-a")
            for name, read in operations(bare_service(MappingStore(path)), revision).items():
                with self.subTest(operation=name):
                    gate = _GatedLstat(path)

                    async def scenario():
                        loop = asyncio.get_running_loop()
                        task = asyncio.ensure_future(read())
                        # Off the loop, the read waits in its worker while the loop runs.
                        entered = await loop.run_in_executor(None, gate.entered.wait, 5)
                        self.assertTrue(entered, f"{name} never reached its read")
                        task.cancel()
                        await asyncio.sleep(0.05)
                        still_owned = not task.done()
                        gate.release.set()
                        (outcome,) = await asyncio.gather(task, return_exceptions=True)
                        return still_owned, outcome

                    rescue = threading.Timer(RESCUE_SECONDS, gate.release.set)
                    rescue.start()
                    try:
                        with gate.patch():
                            still_owned, outcome = asyncio.run(scenario())
                    finally:
                        gate.release.set()
                        rescue.cancel()
                        rescue.join()
                    self.assertNotIn(threading.main_thread(), gate.threads, f"{name} read on the event loop thread")
                    self.assertIsInstance(
                        outcome, asyncio.CancelledError, f"{name} could not be cancelled during its read",
                    )
                    self.assertTrue(still_owned, f"{name} let go of its read while the worker still ran")


class MappingReadResultsAndErrorsUnchangedTests(unittest.TestCase):
    """Controls: off the loop, the same results and the same errors as before."""

    def test_export_and_preview_return_the_published_rows_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            service = bare_service(MappingStore(path))
            bundle = asyncio.run(service.export_mapping_bundle(selected_enclosure_id="shelf-a"))
            self.assertEqual([(row.slot, row.notes) for row in bundle.mappings], [(1, "first"), (2, "second")])
            self.assertEqual(bundle.revision, store.scope_revision("sys-a", "shelf-a"))
            self.assertEqual((bundle.system_id, bundle.enclosure_id), ("sys-a", "shelf-a"))

            preview = asyncio.run(service.preview_mapping_bundle(bundle, selected_enclosure_id="shelf-a"))
            expected = store.preview_replace_mappings(
                "sys-a", "shelf-a", service._rewrite_mapping_bundle(bundle, "shelf-a"),
            )
            self.assertEqual(preview["revision"], expected["revision"])
            self.assertEqual(preview["revision"], bundle.revision)
            for key in ("additions", "updates", "removals"):
                self.assertEqual(preview[key], expected[key])

    def test_store_errors_reach_the_caller_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, _store = seeded_store(directory)
            for error in (MappingScopeConflict(), MappingReadBusy(path), PermissionError("synthetic")):
                service = bare_service(MappingStore(path))
                for name, read in operations(service, "0" * 16).items():
                    with (
                        self.subTest(operation=name, error=type(error).__name__),
                        patch.object(MappingStore, "_read_source_bytes", side_effect=error),
                    ):
                        with self.assertRaises(type(error)) as raised:
                            asyncio.run(read())
                        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
