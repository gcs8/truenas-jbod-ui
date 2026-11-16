"""Two owners of one mapping file must not lose each other's updates (#1228).

Every test uses a synthetic mapping file in a temporary directory.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app import main as app_main
from app import routes as app_routes
from app.models.domain import ManualMapping, MappingBundle, MappingImportConfirmation
from app.services import mapping_store as mapping_store_module
from app.services.mapping_store import MappingRevisionConflict, MappingStorageUnwritable, MappingStore
from app.services.storage_writability import StorageDirectoryUnwritable

try:
    import fcntl
except ImportError:  # pragma: no cover - the suite is POSIX-only (scripts/dev_check.py)
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]
WRITER_LOCK_UNAVAILABLE = "mapping_writer_lock_unavailable"


def mapping(system_id: str, slot: int, notes: str) -> ManualMapping:
    return ManualMapping(system_id=system_id, enclosure_id="shelf", slot=slot, notes=notes)


def system_ids(path: Path) -> list[str]:
    return sorted(row.system_id for row in MappingStore(path).list_mappings())


def seeded_store(directory: str) -> tuple[Path, MappingStore]:
    path = Path(directory) / "slot_mappings.json"
    store = MappingStore(path)
    # Seed a version 2 document so the race is not confused with a migration.
    store.save_mapping(mapping("seed", 0, "seed"))
    return path, store


class TwoOwnersInOneProcessTests(unittest.TestCase):
    """Two store objects on one file, writing from two threads."""

    def race(self, path: Path, first_write, second_write):
        """Let the second owner read before the first owner commits.

        The first owner's read waits (briefly) until the second owner has read
        too; the second owner's read then waits until the first owner has
        finished. Without a shared writer transaction the second owner commits
        a document read before the first commit. With one, the second owner
        cannot read until the first has finished, and both waits time out
        harmlessly.

        The second owner is submitted only after the first owner is inside its
        read, so the first owner is the writer that holds the transaction
        whatever order the pool starts its threads in.
        """
        first, second = MappingStore(path), MappingStore(path)
        first_reading, second_read, first_done = threading.Event(), threading.Event(), threading.Event()
        first_load, second_load = first._load_state, second._load_state

        def first_read():
            state = first_load()
            first_reading.set()
            second_read.wait(timeout=1.0)
            return state

        def second_read_after_first_commit():
            state = second_load()
            second_read.set()
            first_done.wait(timeout=5.0)
            return state

        def run_first():
            try:
                return first_write(first)
            finally:
                first_done.set()

        def outcome(future):
            try:
                future.result(timeout=30)
            except MappingRevisionConflict:
                return "conflict"
            return "success"

        with (
            patch.object(first, "_load_state", first_read),
            patch.object(second, "_load_state", second_read_after_first_commit),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first_future = pool.submit(run_first)
            if not first_reading.wait(timeout=10.0):
                self.fail("the first owner did not reach its read")
            second_future = pool.submit(second_write, second)
            return outcome(first_future), outcome(second_future)

    def test_independent_updates_from_two_owners_both_survive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            revision_a = store.save_revision("a", "shelf", 1)
            revision_b = store.save_revision("b", "shelf", 2)

            results = self.race(
                path,
                lambda owner: owner.save_mapping(mapping("a", 1, "first"), expected_revision=revision_a),
                lambda owner: owner.save_mapping(mapping("b", 2, "second"), expected_revision=revision_b),
            )

            final = system_ids(path)
            # Either both independent updates survive, or the stale one is
            # refused. Two successes with one update missing is the bug.
            if results == ("success", "success"):
                self.assertEqual(final, ["a", "b", "seed"], "both saves reported success but an update was lost")
            else:
                self.assertEqual(results, ("success", "conflict"))
                self.assertEqual(final, ["a", "seed"])

    def test_a_conflicting_stale_update_is_refused_not_silently_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            stale_revision = store.save_revision("a", "shelf", 1)

            results = self.race(
                path,
                lambda owner: owner.save_mapping(mapping("a", 1, "first"), expected_revision=stale_revision),
                lambda owner: owner.save_mapping(mapping("a", 1, "second"), expected_revision=stale_revision),
            )

            self.assertEqual(results, ("success", "conflict"))
            self.assertEqual(MappingStore(path).get_mapping("a", "shelf", 1).notes, "first")

    def test_owners_of_one_file_share_one_in_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slot_mappings.json"
            other = Path(directory) / "other.json"
            aliased = Path(directory) / "." / "slot_mappings.json"
            self.assertIs(MappingStore(path)._lock, MappingStore(aliased)._lock)
            self.assertIsNot(MappingStore(path)._lock, MappingStore(other)._lock)


CHILD_WRITER = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    from app.models.domain import ManualMapping
    from app.services.mapping_store import MappingRevisionConflict, MappingStore

    path, markers, revision = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    store = MappingStore(path)
    load = store._load_state

    def read_then_wait_for_parent():
        state = load()
        (markers / "child-read").touch()
        # Shorter than the parent's bounded lock wait.
        deadline = time.monotonic() + 0.5
        while not (markers / "parent-done").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return state

    store._load_state = read_then_wait_for_parent
    try:
        store.save_mapping(
            ManualMapping(system_id="b", enclosure_id="shelf", slot=2, notes="second"),
            expected_revision=revision,
        )
    except MappingRevisionConflict:
        print("conflict")
    else:
        print("success")
    """
)


@unittest.skipIf(fcntl is None, "the cross-process writer lock needs POSIX flock")
class TwoOwnersInTwoProcessesTests(unittest.TestCase):
    def test_a_second_process_cannot_commit_a_document_read_before_this_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            markers = Path(directory) / "markers"
            markers.mkdir()
            revision_a = store.save_revision("a", "shelf", 1)
            revision_b = store.save_revision("b", "shelf", 2)
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            child = subprocess.Popen(
                [sys.executable, "-c", CHILD_WRITER, str(ROOT), str(path), str(markers), revision_b],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 30
                while not (markers / "child-read").exists():
                    if child.poll() is not None or time.monotonic() > deadline:
                        self.fail(f"child writer did not read: {child.communicate()[1][-2000:]}")
                    time.sleep(0.01)
                try:
                    store.save_mapping(mapping("a", 1, "first"), expected_revision=revision_a)
                    parent_result = "success"
                except MappingRevisionConflict:
                    parent_result = "conflict"
                (markers / "parent-done").touch()
                stdout, stderr = child.communicate(timeout=60)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
            self.assertEqual(child.returncode, 0, stderr[-2000:])
            results = (parent_result, stdout.strip())
            final = system_ids(path)
            if results == ("success", "success"):
                self.assertEqual(final, ["a", "b", "seed"], "both saves reported success but an update was lost")
            else:
                self.assertIn("conflict", results)
                self.assertEqual(len(final), 2)


@unittest.skipIf(fcntl is None, "the cross-process writer lock needs POSIX flock")
class WriterLockRefusalTests(unittest.TestCase):
    """A writer that cannot hold the shared lock refuses with a named reason."""

    def lock_path(self, path: Path) -> Path:
        return path.with_name(path.name + ".lock")

    def test_a_held_lock_refuses_the_save_after_a_bounded_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            before = path.read_bytes()
            store.writer_lock_timeout_seconds = 0.2
            # Another owner (here a separate open file description) holds it.
            holder = os.open(self.lock_path(path), os.O_RDWR | os.O_CREAT, 0o660)
            try:
                fcntl.flock(holder, fcntl.LOCK_EX)
                with self.assertRaises(StorageDirectoryUnwritable) as raised:
                    store.save_mapping(mapping("a", 1, "first"))
                self.assertEqual(raised.exception.error_code, WRITER_LOCK_UNAVAILABLE)
                self.assertEqual(raised.exception.reason, "busy")
                self.assertEqual(path.read_bytes(), before)
            finally:
                os.close(holder)
            # Control: once the other owner lets go, the same save succeeds.
            store.save_mapping(mapping("a", 1, "first"))
            self.assertEqual(system_ids(path), ["a", "seed"])

    def test_every_mutator_takes_the_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            store.save_mapping(mapping("a", 1, "first"))
            preview = store.preview_replace_mappings("a", "shelf", [mapping("a", 3, "imported")])
            before = path.read_bytes()
            store.writer_lock_timeout_seconds = 0.1
            holder = os.open(self.lock_path(path), os.O_RDWR | os.O_CREAT, 0o660)
            mutators = {
                "save": lambda: store.save_mapping(mapping("a", 2, "second")),
                "clear": lambda: store.clear_mapping("a", "shelf", 1),
                "replace": lambda: store.replace_mappings("a", "shelf", [mapping("a", 3, "replaced")]),
                "import": lambda: store.apply_mapping_import(
                    "a",
                    "shelf",
                    [mapping("a", 3, "imported")],
                    expected_revision=preview["revision"],
                    import_digest=preview["import_digest"],
                ),
            }
            try:
                fcntl.flock(holder, fcntl.LOCK_EX)
                for name, mutate in mutators.items():
                    with self.subTest(mutator=name):
                        try:
                            with self.assertRaises(StorageDirectoryUnwritable) as raised:
                                mutate()
                            self.assertEqual(raised.exception.error_code, WRITER_LOCK_UNAVAILABLE)
                            self.assertEqual(raised.exception.reason, "busy")
                            self.assertEqual(path.read_bytes(), before)
                        finally:
                            # Keep each mutator independent of an unguarded
                            # write by the one before it.
                            path.write_bytes(before)
            finally:
                os.close(holder)

    def test_a_filesystem_without_flock_refuses_rather_than_writing_unguarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            before = path.read_bytes()

            def no_lock_manager(descriptor, operation):
                raise OSError(errno.ENOLCK, "No locks available")

            with patch.object(fcntl, "flock", no_lock_manager):
                with self.assertRaises(StorageDirectoryUnwritable) as raised:
                    store.save_mapping(mapping("a", 1, "first"))
            self.assertEqual(raised.exception.error_code, WRITER_LOCK_UNAVAILABLE)
            self.assertEqual(raised.exception.reason, "unsupported")
            self.assertEqual(path.read_bytes(), before)

    def test_a_lock_path_that_is_not_a_regular_file_is_refused(self) -> None:
        for kind in ("symlink", "fifo", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path, store = seeded_store(directory)
                before = path.read_bytes()
                elsewhere = Path(directory) / "elsewhere"
                elsewhere.write_bytes(b"")
                self.lock_path(path).unlink(missing_ok=True)
                if kind == "symlink":
                    self.lock_path(path).symlink_to(elsewhere)
                elif kind == "fifo":
                    os.mkfifo(self.lock_path(path))
                else:
                    self.lock_path(path).mkdir()

                with self.assertRaises(StorageDirectoryUnwritable) as raised:
                    store.save_mapping(mapping("a", 1, "first"))
                self.assertEqual(raised.exception.error_code, WRITER_LOCK_UNAVAILABLE)
                self.assertEqual(raised.exception.reason, "not_regular")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(elsewhere.read_bytes(), b"")

                # Control: once the obstruction is gone the same save succeeds.
                if kind == "directory":
                    self.lock_path(path).rmdir()
                else:
                    self.lock_path(path).unlink()
                store.save_mapping(mapping("a", 1, "first"))
                self.assertEqual(system_ids(path), ["a", "seed"])

    def test_reads_neither_create_nor_wait_for_the_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slot_mappings.json"
            fresh = MappingStore(path)
            self.assertEqual(fresh.list_mappings(), [])
            self.assertFalse(self.lock_path(path).exists())

            path, store = seeded_store(directory)
            store.writer_lock_timeout_seconds = 0.1
            holder = os.open(self.lock_path(path), os.O_RDWR | os.O_CREAT, 0o660)
            try:
                fcntl.flock(holder, fcntl.LOCK_EX)
                self.assertEqual([row.system_id for row in store.list_mappings()], ["seed"])
                self.assertTrue(store.save_revision("a", "shelf", 1))
            finally:
                os.close(holder)

    def test_the_lock_is_a_stable_sidecar_not_the_replaced_mapping_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            self.assertTrue(self.lock_path(path).is_file(), "a save leaves no writer lock file beside the mappings")
            lock_identity = os.stat(self.lock_path(path)).st_ino
            mapping_identity = os.stat(path).st_ino
            store.save_mapping(mapping("a", 1, "first"))
            self.assertEqual(os.stat(self.lock_path(path)).st_ino, lock_identity)
            self.assertNotEqual(os.stat(path).st_ino, mapping_identity)
            self.assertEqual(list(path.parent.glob(f"{path.name}.*.tmp")), [])



class StalledInProcessWriterTests(unittest.TestCase):
    """One owner's stalled writer must not hold up another owner's reads or writes."""

    def stall_first_owner(self, path: Path):
        """Start a save on a first owner and hold it inside its writer transaction."""
        owner = MappingStore(path)
        entered, release = threading.Event(), threading.Event()
        load = owner._load_state

        def paused_load():
            state = load()
            entered.set()
            release.wait(timeout=30)
            return state

        owner._load_state = paused_load  # type: ignore[method-assign]
        return owner, entered, release

    def test_reads_finish_while_another_owner_holds_the_writer_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            owner, entered, release = self.stall_first_owner(path)
            reader = MappingStore(path)
            incoming = [mapping("b", 3, "imported")]
            reads = {
                "load_read_snapshot": reader.load_read_snapshot,
                "list_mappings": reader.list_mappings,
                "save_revision": lambda: reader.save_revision("b", "shelf", 2),
                "clear_revision": lambda: reader.clear_revision("b", "shelf", 2),
                "scope_revision": lambda: reader.scope_revision("b", "shelf"),
                "save_revisions": lambda: reader.save_revisions("b", [("shelf", 2)]),
                "clear_revisions": lambda: reader.clear_revisions("b", [("shelf", 2)]),
                "preview_replace_mappings": lambda: reader.preview_replace_mappings("b", "shelf", incoming),
                # The stalled owner's own store object reads without waiting too.
                "same_owner_save_revision": lambda: owner.save_revision("b", "shelf", 2),
            }
            with ThreadPoolExecutor(max_workers=len(reads) + 1) as pool:
                stalled = pool.submit(owner.save_mapping, mapping("a", 1, "first"))
                try:
                    self.assertTrue(entered.wait(timeout=10), "the first owner did not reach its read")
                    futures = {name: pool.submit(read) for name, read in reads.items()}
                    wait(futures.values(), timeout=5)
                    for name, future in futures.items():
                        with self.subTest(read=name):
                            self.assertTrue(future.done(), f"{name} waited for another owner's writer")
                            self.assertIsNone(future.exception())
                finally:
                    release.set()
                stalled.result(timeout=30)
            self.assertEqual(system_ids(path), ["a", "seed"])

    def test_a_second_writer_refuses_after_the_bounded_wait_instead_of_waiting_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            owner, entered, release = self.stall_first_owner(path)
            second = MappingStore(path)
            second.writer_lock_timeout_seconds = 0.1
            with ThreadPoolExecutor(max_workers=2) as pool:
                stalled = pool.submit(owner.save_mapping, mapping("a", 1, "first"))
                try:
                    self.assertTrue(entered.wait(timeout=10), "the first owner did not reach its read")
                    before = path.read_bytes()
                    started = time.monotonic()
                    waiting = pool.submit(second.save_mapping, mapping("b", 2, "second"))
                    wait([waiting], timeout=5)
                    self.assertTrue(waiting.done(), "the second writer waited without bound for the first")
                    with self.assertRaises(StorageDirectoryUnwritable) as raised:
                        waiting.result()
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertEqual(raised.exception.error_code, WRITER_LOCK_UNAVAILABLE)
                    self.assertEqual(raised.exception.reason, "busy")
                    self.assertEqual(path.read_bytes(), before)
                finally:
                    release.set()
                stalled.result(timeout=30)
            # Control: with the first owner finished, the same save succeeds.
            second.save_mapping(mapping("b", 2, "second"))
            self.assertEqual(system_ids(path), ["a", "b", "seed"])


def bare_inventory_service(store: MappingStore):
    """The real async mapping methods with only their appliance reads replaced."""
    from app.config import SystemConfig
    from app.models.domain import InventorySnapshot
    from app.services.inventory import InventoryService

    service = object.__new__(InventoryService)
    service.system = SystemConfig(id="a")
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


@unittest.skipIf(fcntl is None, "the cross-process writer lock needs POSIX flock")
class MappingWritesLeaveTheEventLoopFreeTests(unittest.TestCase):
    """Waiting for another process's writer lock must not stall the UI event loop."""

    def lock_path(self, path: Path) -> Path:
        return path.with_name(path.name + ".lock")

    def test_save_clear_and_import_wait_for_the_lock_off_the_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            store.save_mapping(mapping("a", 1, "first"))
            store.writer_lock_timeout_seconds = 0.6
            service = bare_inventory_service(store)
            bundle = MappingBundle(mappings=[mapping("a", 3, "imported")])
            preview = store.preview_replace_mappings("a", "shelf", service._rewrite_mapping_bundle(bundle, "shelf"))
            writes = {
                "save": lambda: service.save_mapping(1, {"notes": "changed"}, selected_enclosure_id="shelf"),
                "clear": lambda: service.clear_mapping(1, selected_enclosure_id="shelf"),
                "import": lambda: service.import_mapping_bundle(
                    bundle,
                    selected_enclosure_id="shelf",
                    expected_revision=preview["revision"],
                    import_digest=preview["import_digest"],
                ),
            }
            before = path.read_bytes()

            async def heartbeat_during(write):
                loop = asyncio.get_running_loop()
                started = loop.time()
                ticks: list[float] = []
                loop.call_later(0.02, lambda: ticks.append(loop.time() - started))
                with self.assertRaises(StorageDirectoryUnwritable) as raised:
                    await write()
                return ticks, raised.exception

            holder = os.open(self.lock_path(path), os.O_RDWR | os.O_CREAT, 0o660)
            try:
                fcntl.flock(holder, fcntl.LOCK_EX)
                for name, write in writes.items():
                    with self.subTest(write=name):
                        ticks, refusal = asyncio.run(heartbeat_during(write))
                        self.assertEqual(refusal.error_code, WRITER_LOCK_UNAVAILABLE)
                        self.assertEqual(refusal.reason, "busy")
                        self.assertTrue(ticks, "a timer due during the lock wait never ran")
                        self.assertLess(ticks[0], 0.3, "the lock wait blocked the event loop")
                        self.assertEqual(path.read_bytes(), before)
            finally:
                os.close(holder)

    def test_a_cancelled_save_still_finishes_and_invalidates_what_it_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            store.writer_lock_timeout_seconds = 5.0
            service = bare_inventory_service(store)
            holder = os.open(self.lock_path(path), os.O_RDWR | os.O_CREAT, 0o660)
            fcntl.flock(holder, fcntl.LOCK_EX)
            # Another owner lets go shortly after the caller has gone away.
            releaser = threading.Timer(0.3, fcntl.flock, args=(holder, fcntl.LOCK_UN))

            async def cancel_while_waiting():
                releaser.start()
                task = asyncio.create_task(
                    service.save_mapping(
                        1, {"notes": "first"}, selected_enclosure_id="shelf", invalidate_snapshot=False,
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    return await task
                except asyncio.CancelledError:
                    return "cancelled"
                except Exception as exc:  # noqa: BLE001 - reported by the assertion below
                    return type(exc).__name__

            try:
                outcome = asyncio.run(cancel_while_waiting())
            finally:
                releaser.cancel()
                releaser.join()
                os.close(holder)
            self.assertEqual(outcome, "cancelled")
            # The write was not abandoned half way: it finished, and the view it
            # changed was invalidated even though the caller went away.
            self.assertEqual(system_ids(path), ["a", "seed"])
            service.invalidate_physical_enclosure_snapshot_cache.assert_called_once()


MAPPING_READ_BUSY = "mapping_read_busy"


def read_busy_refusal_type(test: unittest.TestCase) -> type[Exception]:
    refusal = getattr(mapping_store_module, "MappingReadBusy", None)
    if refusal is None:
        test.fail("the mapping store has no named refusal for a read that keeps meeting newer files")
    return refusal


def stalled_rename(path: Path):
    """Hold a writer inside its os.replace onto `path` until released."""
    entered, release = threading.Event(), threading.Event()
    original_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if Path(destination) == path:
            entered.set()
            if not release.wait(timeout=30):
                raise AssertionError("the test never released the stalled rename")
        return original_replace(source, destination, *args, **kwargs)

    return patch.object(mapping_store_module.os, "replace", replace), entered, release


class ReadsNeverWaitForAPublishingWriterTests(unittest.TestCase):
    """A reader never waits for a writer, not even one stalled inside its rename."""

    def test_reads_return_the_last_published_content_while_a_rename_is_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, _store = seeded_store(directory)
            owner, reader = MappingStore(path), MappingStore(path)
            published = path.read_bytes()
            reads = {
                "load_read_snapshot": lambda: len(reader.load_read_snapshot()),
                "list_mappings": lambda: sorted(row.system_id for row in reader.list_mappings()),
                "get_mapping": lambda: reader.get_mapping("a", "shelf", 1),
                "load_all": lambda: len(reader.load_all()),
                "load_lookup_snapshot": lambda: len(reader.load_lookup_snapshot()),
                "save_revision": lambda: bool(reader.save_revision("b", "shelf", 2)),
                "preview_replace_mappings": lambda: bool(
                    reader.preview_replace_mappings("b", "shelf", [mapping("b", 3, "imported")])["revision"]
                ),
            }
            stall, entered, release = stalled_rename(path)
            with stall, ThreadPoolExecutor(max_workers=len(reads) + 1) as pool:
                writer = pool.submit(owner.save_mapping, mapping("a", 1, "first"))
                try:
                    self.assertTrue(entered.wait(timeout=10), "the writer did not reach its rename")
                    futures = {name: pool.submit(read) for name, read in reads.items()}
                    wait(futures.values(), timeout=2)
                    for name, future in futures.items():
                        with self.subTest(read=name):
                            self.assertTrue(future.done(), f"{name} waited for a writer stalled in its rename")
                    self.assertEqual(path.read_bytes(), published)
                finally:
                    release.set()
                writer.result(timeout=30)
                for future in futures.values():
                    self.assertIsNone(future.exception(timeout=30))
            # Each read returned the last published content, not the stalled writer's.
            self.assertEqual(futures["list_mappings"].result(), ["seed"])
            self.assertIsNone(futures["get_mapping"].result())
            self.assertEqual(futures["load_all"].result(), 1)
            self.assertEqual(system_ids(path), ["a", "seed"])

    def test_the_event_loop_keeps_running_while_a_rename_is_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, _store = seeded_store(directory)
            owner, reader = MappingStore(path), MappingStore(path)
            service = bare_inventory_service(reader)
            # An empty bundle writes no bays, so the preview reads no appliance.
            bundle = MappingBundle(mappings=[])
            reads = {
                "preview": lambda: service.preview_mapping_bundle(bundle, selected_enclosure_id="shelf"),
                "export": lambda: service.export_mapping_bundle(selected_enclosure_id="shelf"),
            }

            async def heartbeat_during(read):
                loop = asyncio.get_running_loop()
                started = loop.time()
                ticks: list[float] = []
                loop.call_later(0.02, lambda: ticks.append(loop.time() - started))
                result = await read()
                elapsed = loop.time() - started
                await asyncio.sleep(0.03)
                return ticks, elapsed, result

            for name, read in reads.items():
                with self.subTest(read=name):
                    stall, entered, release = stalled_rename(path)
                    # Only a thread outside the loop can rescue a blocked loop;
                    # it fires long after a prompt read would have returned.
                    rescue = threading.Timer(1.0, release.set)
                    with stall, ThreadPoolExecutor(max_workers=1) as pool:
                        writer = pool.submit(owner.save_mapping, mapping("a", 1, name))
                        try:
                            self.assertTrue(entered.wait(timeout=10), "the writer did not reach its rename")
                            rescue.start()
                            ticks, elapsed, result = asyncio.run(heartbeat_during(read))
                        finally:
                            release.set()
                            rescue.cancel()
                            rescue.join()
                        writer.result(timeout=30)
                    self.assertTrue(ticks, "a timer due during the read never ran")
                    self.assertLess(ticks[0], 0.3, "the read blocked the event loop behind a stalled rename")
                    self.assertLess(elapsed, 0.5, "the read waited for a writer stalled in its rename")
                    if name == "export":
                        # The last published content: the preview round's save,
                        # not the export round's, which was still in its rename.
                        self.assertEqual([row.notes for row in result.mappings], ["preview"])

    def test_a_writer_does_not_wait_for_a_reader_paused_between_its_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, _store = seeded_store(directory)
            owner, reader = MappingStore(path), MappingStore(path)
            paused, writer_done = threading.Event(), threading.Event()
            reader_observations: list[Path] = []
            original_lstat = os.lstat

            def lstat(name, *args, **kwargs):
                result = original_lstat(name, *args, **kwargs)
                if threading.current_thread().name == "paused-reader" and Path(name) == path:
                    reader_observations.append(Path(name))
                    if not paused.is_set():
                        paused.set()
                        writer_done.wait(timeout=5)
                return result

            outcome: dict[str, object] = {}

            def read() -> None:
                try:
                    outcome["ids"] = sorted(row.system_id for row in reader.list_mappings())
                except Exception as exc:  # noqa: BLE001 - reported by the assertions below
                    outcome["error"] = exc

            with patch.object(mapping_store_module.os, "lstat", lstat), ThreadPoolExecutor(max_workers=1) as pool:
                reading = threading.Thread(target=read, name="paused-reader")
                reading.start()
                try:
                    self.assertTrue(paused.wait(timeout=10), "the reader did not reach its first check")
                    writer = pool.submit(owner.save_mapping, mapping("a", 1, "first"))
                    wait([writer], timeout=2)
                    self.assertTrue(writer.done(), "the writer waited for a reader paused between its checks")
                    self.assertIsNone(writer.exception())
                finally:
                    writer_done.set()
                    reading.join(timeout=30)
            self.assertFalse(reading.is_alive())
            self.assertNotIn("error", outcome)
            # The reader met the newer file, reread it once and returned it whole.
            self.assertEqual(outcome["ids"], ["a", "seed"])
            self.assertEqual(len(reader_observations), 2)

    def churn(self, path: Path, swaps: int | None):
        """Replace the file with a fresh inode just before each source open."""
        original_open, original_replace = os.open, os.replace
        content = path.read_bytes()
        opens: list[int] = []

        def open_after_swap(name, flags, *args, **kwargs):
            if Path(name) == path and not flags & os.O_CREAT:
                opens.append(1)
                if swaps is None or len(opens) <= swaps:
                    fresh = path.with_name("churn.json")
                    fresh.write_bytes(content)
                    original_replace(fresh, path)
            return original_open(name, flags, *args, **kwargs)

        return patch.object(mapping_store_module.os, "open", open_after_swap), opens, content

    def test_a_read_that_keeps_meeting_newer_files_refuses_by_name_after_three_rereads(self) -> None:
        refusal_type = read_busy_refusal_type(self)
        reads = {
            "load_read_snapshot": lambda store: store.load_read_snapshot(),
            "list_mappings": lambda store: store.list_mappings(),
            # The tolerant display reads must not turn the refusal into "no mappings".
            "get_mapping": lambda store: store.get_mapping("seed", "shelf", 0),
            "load_all": lambda store: store.load_all(),
            "load_lookup_snapshot": lambda store: store.load_lookup_snapshot(),
            "save_revision": lambda store: store.save_revision("b", "shelf", 2),
        }
        for name, read in reads.items():
            with self.subTest(read=name), tempfile.TemporaryDirectory() as directory:
                path, _store = seeded_store(directory)
                reader = MappingStore(path)
                churn, opens, content = self.churn(path, swaps=None)
                with churn, self.assertRaises(refusal_type) as raised:
                    read(reader)
                self.assertEqual(raised.exception.error_code, MAPPING_READ_BUSY)
                self.assertEqual(raised.exception.reason, "busy")
                self.assertEqual(len(opens), 4, "one read and three rereads, then the refusal")
                self.assertEqual(path.read_bytes(), content)

                # Control: three newer files in a row are still read through.
                churn, opens, content = self.churn(path, swaps=3)
                with churn:
                    if name == "get_mapping":
                        self.assertEqual(read(reader).notes, "seed")
                    else:
                        self.assertTrue(read(reader))
                self.assertEqual(len(opens), 4)

    def test_a_writer_still_refuses_a_file_replaced_during_its_own_read(self) -> None:
        # Inside the writer transaction no writer that follows the protocol can
        # replace the file, so a change there is refused at once, as before.
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            churn, opens, content = self.churn(path, swaps=None)
            with churn, self.assertRaises(OSError):
                store.save_mapping(mapping("a", 1, "first"))
            self.assertEqual(len(opens), 1)
            self.assertEqual(path.read_bytes(), content)

    def test_a_reread_still_refuses_a_file_replaced_by_a_link(self) -> None:
        refusal_type = read_busy_refusal_type(self)
        original_open = os.open
        with tempfile.TemporaryDirectory() as directory:
            path, _store = seeded_store(directory)
            reader = MappingStore(path)
            elsewhere = Path(directory) / "elsewhere.json"
            elsewhere.write_bytes(path.read_bytes())
            opens: list[int] = []

            def link_then_open(name, flags, *args, **kwargs):
                if Path(name) == path and not flags & os.O_CREAT:
                    opens.append(1)
                    if len(opens) == 1:
                        path.unlink()
                        path.symlink_to(elsewhere)
                return original_open(name, flags, *args, **kwargs)

            with patch.object(mapping_store_module.os, "open", link_then_open):
                with self.assertRaises(OSError) as raised:
                    reader.list_mappings()
            # The first open met the link (O_NOFOLLOW) and read again; the
            # reread's own check refused the link without opening it.
            self.assertNotIsInstance(raised.exception, refusal_type)
            self.assertIn("not a regular file", str(raised.exception))
            self.assertEqual(len(opens), 1)

    def test_a_vanished_or_stale_handle_is_reread(self) -> None:
        original_open, original_fstat, original_read = os.open, os.fstat, os.read
        original_lstat = os.lstat
        stale = getattr(errno, "ESTALE", errno.EIO)
        for case in ("vanished_open", "stale_lstat", "stale_open", "stale_fstat", "stale_read"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path, _store = seeded_store(directory)
                reader = MappingStore(path)
                descriptors: list[int] = []
                failed: list[str] = []

                def source_open(name, flags, *args, **kwargs):
                    if Path(name) == path and not flags & os.O_CREAT:
                        if not failed and case == "vanished_open":
                            failed.append(case)
                            raise FileNotFoundError(errno.ENOENT, "synthetic vanished file")
                        if not failed and case == "stale_open":
                            failed.append(case)
                            raise OSError(stale, "synthetic stale handle")
                        descriptor = original_open(name, flags, *args, **kwargs)
                        descriptors.append(descriptor)
                        return descriptor
                    return original_open(name, flags, *args, **kwargs)

                def source_lstat(name, *args, **kwargs):
                    if case == "stale_lstat" and not failed and Path(name) == path:
                        failed.append(case)
                        raise OSError(stale, "synthetic stale handle")
                    return original_lstat(name, *args, **kwargs)

                def source_fstat(descriptor, *args, **kwargs):
                    if case == "stale_fstat" and not failed and descriptor in descriptors:
                        failed.append(case)
                        raise OSError(stale, "synthetic stale handle")
                    return original_fstat(descriptor, *args, **kwargs)

                def source_read(descriptor, size):
                    if case == "stale_read" and not failed and descriptor in descriptors:
                        failed.append(case)
                        raise OSError(stale, "synthetic stale handle")
                    return original_read(descriptor, size)

                with (
                    patch.object(mapping_store_module.os, "lstat", source_lstat),
                    patch.object(mapping_store_module.os, "open", source_open),
                    patch.object(mapping_store_module.os, "fstat", source_fstat),
                    patch.object(mapping_store_module.os, "read", source_read),
                ):
                    self.assertEqual([row.system_id for row in reader.list_mappings()], ["seed"])
                self.assertEqual(failed, [case])
                # Every descriptor the reads opened is closed again.
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        original_fstat(descriptor)


@unittest.skipIf(not hasattr(os, "mkfifo"), "swapping in a FIFO needs POSIX mkfifo")
class ReadsRefuseANonRegularFileSwappedInAfterTheirCheckTests(unittest.TestCase):
    """A path swapped to a FIFO, directory or link after the check never blocks or reads empty."""

    def swap_after_check(self, path: Path, elsewhere: Path, kind: str):
        original_open = os.open
        swapped = threading.Event()

        def swap_then_open(name, flags, *args, **kwargs):
            if Path(name) == path and not flags & os.O_CREAT and not swapped.is_set():
                path.unlink()
                if kind == "fifo":
                    os.mkfifo(path)
                elif kind == "directory":
                    path.mkdir()
                else:
                    path.symlink_to(elsewhere)
                swapped.set()
            return original_open(name, flags, *args, **kwargs)

        return patch.object(mapping_store_module.os, "open", swap_then_open), swapped

    def test_a_swap_after_the_check_is_refused_promptly_by_every_reader(self) -> None:
        refusal_type = read_busy_refusal_type(self)
        reads = {
            "load_read_snapshot": lambda reader: reader.load_read_snapshot(),
            "list_mappings": lambda reader: reader.list_mappings("a", "shelf"),
            "scope_revision": lambda reader: reader.scope_revision("a", "shelf"),
            "preview_replace_mappings": lambda reader: reader.preview_replace_mappings("a", "shelf", []),
            "load_all": lambda reader: reader.load_all(),
            "load_lookup_snapshot": lambda reader: reader.load_lookup_snapshot(),
        }
        for kind in ("fifo", "directory", "symlink"):
            for name, read in reads.items():
                with self.subTest(kind=kind, read=name), tempfile.TemporaryDirectory() as directory:
                    path, _store = seeded_store(directory)
                    reader = MappingStore(path)
                    elsewhere = Path(directory) / "elsewhere.json"
                    elsewhere.write_bytes(path.read_bytes())
                    outcome: dict[str, object] = {}

                    def run() -> None:
                        try:
                            outcome["returned"] = read(reader)
                        except BaseException as exc:  # noqa: BLE001 - reported below
                            outcome["raised"] = exc

                    swap, swapped = self.swap_after_check(path, elsewhere, kind)
                    with swap:
                        thread = threading.Thread(target=run, daemon=True)
                        thread.start()
                        self.assertTrue(swapped.wait(timeout=5), "the read never reached its open")
                        thread.join(timeout=2)
                        blocked = thread.is_alive()
                        if blocked:
                            # Rescue the blocked open so the run cannot hang.
                            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                            thread.join(timeout=5)
                    self.assertFalse(blocked, f"{name} blocked opening a {kind} swapped in after its check")
                    raised = outcome.get("raised")
                    self.assertIsInstance(
                        raised, OSError, f"{name} returned {outcome.get('returned')!r} for a {kind}, not a refusal",
                    )
                    self.assertNotIsInstance(raised, refusal_type)
                    self.assertIn("not a regular file", str(raised))

    def test_a_regular_file_already_in_place_still_reads_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            store.save_mapping(mapping("a", 1, "kept"))
            reader = MappingStore(path)
            self.assertEqual([row.notes for row in reader.list_mappings("a", "shelf")], ["kept"])
            self.assertEqual(len(reader.load_all()), 2)
            self.assertEqual(len(reader.load_lookup_snapshot()), 2)
            self.assertEqual(len(reader.load_read_snapshot()), 2)


class MappingExportReadsOneVersionTests(unittest.TestCase):
    """An export's rows and its revision come from the same published version."""

    def test_a_save_published_during_an_export_does_not_mix_two_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            store.save_mapping(mapping("a", 1, "A"))
            revision_a = store.scope_revision("a", "shelf")
            reader = MappingStore(path)
            service = bare_inventory_service(reader)
            original_read = MappingStore._read_source_bytes_once
            reads: list[int] = []

            def read_then_publish(self_store):
                content = original_read(self_store)
                if self_store is reader:
                    reads.append(1)
                    if len(reads) == 1:
                        store.save_mapping(mapping("a", 1, "B"))
                return content

            with patch.object(MappingStore, "_read_source_bytes_once", read_then_publish):
                bundle = asyncio.run(service.export_mapping_bundle("shelf"))
            revision_b = store.scope_revision("a", "shelf")
            self.assertNotEqual(revision_a, revision_b)
            self.assertEqual([row.notes for row in bundle.mappings], ["A"])
            self.assertEqual(bundle.revision, revision_a, "the export paired version A's rows with another revision")
            self.assertEqual(len(reads), 1, "the export read the mapping file more than once")

    def test_an_export_with_no_concurrent_save_matches_the_current_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            store.save_mapping(mapping("a", 1, "A"))
            store.save_mapping(mapping("a", 2, "A2"))
            service = bare_inventory_service(MappingStore(path))
            bundle = asyncio.run(service.export_mapping_bundle("shelf"))
            self.assertEqual([(row.slot, row.notes) for row in bundle.mappings], [(1, "A"), (2, "A2")])
            self.assertEqual(bundle.revision, store.scope_revision("a", "shelf"))
            self.assertEqual(
                bundle.revision,
                store.preview_replace_mappings("a", "shelf", bundle.mappings)["revision"],
            )
            self.assertEqual(bundle.system_id, "a")
            self.assertEqual(bundle.enclosure_id, "shelf")


@unittest.skipIf(not hasattr(os, "mkfifo"), "swapping in a FIFO needs POSIX mkfifo")
@unittest.skipIf(fcntl is None, "the cross-process writer lock needs POSIX flock")
class WriterReadbackRefusesANonRegularFileTests(unittest.TestCase):
    """The writer's readback after publishing uses the checked, non-blocking read."""

    def test_a_non_regular_file_at_readback_is_refused_promptly_and_frees_the_lock(self) -> None:
        durability_error = mapping_store_module.MappingDurabilityError
        for kind in ("fifo", "directory", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path, store = seeded_store(directory)
                elsewhere = Path(directory) / "elsewhere.json"
                original_fsync = store._fsync_parent_directory

                def swap_after_publish() -> None:
                    original_fsync()
                    # The symlink target holds exactly the published bytes, so a
                    # readback that follows links would accept it.
                    elsewhere.write_bytes(path.read_bytes())
                    path.unlink()
                    if kind == "fifo":
                        os.mkfifo(path)
                    elif kind == "directory":
                        path.mkdir()
                    else:
                        path.symlink_to(elsewhere)

                outcome: dict[str, object] = {}

                def run() -> None:
                    try:
                        outcome["returned"] = store.save_mapping(mapping("a", 1, "first"))
                    except BaseException as exc:  # noqa: BLE001 - reported below
                        outcome["raised"] = exc

                with patch.object(store, "_fsync_parent_directory", swap_after_publish):
                    thread = threading.Thread(target=run, daemon=True)
                    thread.start()
                    thread.join(timeout=2)
                    blocked = thread.is_alive()
                    if blocked:
                        # Rescue the blocked readback so the run cannot hang.
                        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                        thread.join(timeout=5)
                self.assertFalse(blocked, f"the writer's readback blocked on a {kind} at the mapping path")
                self.assertIsInstance(
                    outcome.get("raised"),
                    durability_error,
                    f"the readback accepted a {kind}: {outcome!r}",
                )
                # The lock is free: another owner takes it at once, in this
                # process and across processes.
                holder = os.open(path.with_name(path.name + ".lock"), os.O_RDWR)
                try:
                    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(holder, fcntl.LOCK_UN)
                finally:
                    os.close(holder)
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink()
                path.write_bytes(elsewhere.read_bytes())
                other = MappingStore(path)
                other.writer_lock_timeout_seconds = 0.2
                other.save_mapping(mapping("b", 2, "second"))
                self.assertEqual(system_ids(path), ["a", "b", "seed"])

    def test_a_normal_save_is_unchanged_byte_for_byte_and_in_its_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            published: list[bytes] = []
            original_replace = store._replace_temp_file

            def capture_then_replace(temp_path: Path) -> None:
                published.append(temp_path.read_bytes())
                original_replace(temp_path)

            row = mapping("a", 1, "kept")
            with patch.object(store, "_replace_temp_file", capture_then_replace):
                returned = store.save_mapping(row)
            self.assertEqual(len(published), 1)
            self.assertEqual(path.read_bytes(), published[0])
            fresh = MappingStore(path)
            self.assertEqual(
                [saved.model_dump() for saved in fresh.list_mappings("a", "shelf")],
                [returned.model_dump()],
            )
            self.assertEqual((returned.system_id, returned.slot, returned.notes), ("a", 1, "kept"))
            self.assertEqual(store.save_revision("a", "shelf", 1), fresh.save_revision("a", "shelf", 1))
            self.assertEqual(store.scope_revision("a", "shelf"), fresh.scope_revision("a", "shelf"))
            self.assertEqual(system_ids(path), ["a", "seed"])


class MappingReadRefusalRouteTests(unittest.TestCase):
    """A read that refuses reaches the browser as a named, retryable 503."""

    def test_a_read_refusal_is_a_named_503_without_a_path(self) -> None:
        refusal_type = read_busy_refusal_type(self)
        refusal = refusal_type(Path("/app/data/slot_mappings.json"))
        route = next(
            route for route in app_main.app.routes if getattr(route, "path", None) == "/api/mappings/export"
        )
        service = Mock()
        service.export_mapping_bundle = AsyncMock(side_effect=refusal)
        registry = Mock()
        registry.get_service.return_value = service
        with patch.object(app_routes, "get_inventory_registry", return_value=registry):
            # The route does not turn the refusal into an empty export.
            with self.assertRaises(refusal_type):
                asyncio.run(route.endpoint(system_id="system-a", enclosure_id="enc-a"))
        handler = app_main.app.exception_handlers.get(refusal_type)
        self.assertIsNotNone(handler, "no handler answers the read refusal; it would be a generic 500")
        with self.assertLogs("app.main", level="WARNING") as logged:
            response = asyncio.run(handler(Mock(), refusal))
        # The service log names the refused read; the response does not.
        self.assertIn("Mapping read refused", "\n".join(logged.output))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("Retry-After"), "1")
        body = json.loads(response.body)
        self.assertEqual(body, {"ok": False, "error": MAPPING_READ_BUSY, "detail": refusal_type.public_detail})
        self.assertNotIn("/app/data", response.body.decode("utf-8"))


class MappingImportRouteRefusalTests(unittest.TestCase):
    """The import route answers a storage or writer-lock refusal like a save does."""

    def test_import_route_returns_the_named_503_instead_of_a_generic_500(self) -> None:
        route = next(
            route for route in app_main.app.routes if getattr(route, "path", None) == "/api/mappings/import"
        )
        payload = MappingImportConfirmation(
            bundle=MappingBundle(mappings=[]),
            expected_revision="a" * 64,
            import_digest="b" * 64,
            confirmed=True,
        )

        def lock_refusal() -> Exception:
            refusal = getattr(mapping_store_module, "MappingWriterLockUnavailable", None)
            if refusal is None:
                self.fail("the mapping store has no named writer-lock refusal")
            return refusal(Path("/app/data/slot_mappings.json.lock"), "busy")

        refusals = {
            "data_directory_unwritable": lambda: MappingStorageUnwritable(Path("/app/data")),
            WRITER_LOCK_UNAVAILABLE: lock_refusal,
        }
        for error_code, make_error in refusals.items():
            with self.subTest(error_code=error_code):
                error = make_error()
                service = Mock()
                service.import_mapping_bundle = AsyncMock(side_effect=error)
                service.get_snapshot = AsyncMock()
                registry = Mock()
                registry.get_service.return_value = service
                with patch.object(app_routes, "get_inventory_registry", return_value=registry):
                    try:
                        response = asyncio.run(
                            route.endpoint(payload=payload, system_id="system-a", enclosure_id="enc-a")
                        )
                    except StorageDirectoryUnwritable:
                        self.fail("the import route let a named storage refusal escape as a generic 500")
                self.assertEqual(response.status_code, 503)
                body = json.loads(response.body)
                self.assertEqual(body["error"], error_code)
                self.assertEqual(body["detail"], type(error).public_detail)
                self.assertNotIn("/app/data", response.body.decode("utf-8"))
                service.get_snapshot.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
