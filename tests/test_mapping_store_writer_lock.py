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
from concurrent.futures import ThreadPoolExecutor
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
        """
        first, second = MappingStore(path), MappingStore(path)
        second_read, first_done = threading.Event(), threading.Event()
        first_load, second_load = first._load_state, second._load_state

        def first_read():
            state = first_load()
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
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path, store = seeded_store(directory)
                before = path.read_bytes()
                elsewhere = Path(directory) / "elsewhere"
                elsewhere.write_bytes(b"")
                self.lock_path(path).unlink(missing_ok=True)
                if kind == "symlink":
                    self.lock_path(path).symlink_to(elsewhere)
                else:
                    os.mkfifo(self.lock_path(path))

                with self.assertRaises(StorageDirectoryUnwritable) as raised:
                    store.save_mapping(mapping("a", 1, "first"))
                self.assertEqual(raised.exception.error_code, WRITER_LOCK_UNAVAILABLE)
                self.assertEqual(raised.exception.reason, "not_regular")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(elsewhere.read_bytes(), b"")

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
