from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.models.domain import SasFabricAlias
from app.services.atomic_write import temporary_path_for, write_atomically
from app.services.sas_fabric_alias_store import SasFabricAliasStore
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore
from app.services.ssh_key_manager import SSHKeyManager
from app.services import upgrade_notice

ITERATIONS = 150


def _temporaries(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.name.endswith(".tmp"))


@contextmanager
def _guard(value: bool):
    yield value


class AtomicWriteHelperTests(unittest.TestCase):
    def test_each_write_stages_a_unique_sibling_that_matches_tmp_ignore_rules(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "store.json"
            first, second = temporary_path_for(target), temporary_path_for(target)
            self.assertNotEqual(first, second)
            for candidate in (first, second):
                self.assertEqual(candidate.parent, target.parent)
                self.assertTrue(candidate.name.startswith("store.json."))
                self.assertTrue(candidate.name.endswith(".tmp"))
            staged: list[Path] = []
            real_replace = Path.replace

            def replace(source: Path, destination: Path) -> Path:
                staged.append(source)
                return real_replace(source, destination)

            with patch.object(Path, "replace", replace):
                for value in (1, 2):
                    self.assertTrue(write_atomically(target, lambda handle, v=value: json.dump({"v": v}, handle)))
            self.assertEqual(len(set(staged)), 2)
            self.assertNotIn(target.with_suffix(".tmp"), staged)
            self.assertEqual(json.loads(target.read_text()), {"v": 2})
            self.assertEqual(_temporaries(Path(raw)), [])

    def test_leaves_an_unowned_fixed_name_temporary_alone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "store.json"
            sibling = target.with_suffix(".tmp")
            sibling.write_text("another writer's stage", encoding="utf-8")
            write_atomically(target, lambda handle: handle.write("{}"))
            self.assertEqual(sibling.read_text(encoding="utf-8"), "another writer's stage")

    def test_failures_remove_the_temporary_and_keep_the_target(self) -> None:
        def failing_write(handle) -> None:
            handle.write('{"partial"')
            raise ValueError("synthetic serialization failure")

        cases = {
            "write": (failing_write, None, ValueError),
            "fsync": (lambda handle: handle.write("{}"), (os, "fsync", OSError(errno.EIO, "synthetic fsync")), OSError),
            "replace": (lambda handle: handle.write("{}"), (Path, "replace", OSError(errno.ENOSPC, "synthetic")), OSError),
        }
        for name, (writer, fault, expected) in cases.items():
            with self.subTest(stage=name), tempfile.TemporaryDirectory() as raw:
                target = Path(raw) / "store.json"
                target.write_text('{"old": true}', encoding="utf-8")
                with patch.object(fault[0], fault[1], side_effect=fault[2]) if fault else _guard(True):
                    with self.assertRaises(expected):
                        write_atomically(target, writer)
                self.assertEqual(json.loads(target.read_text()), {"old": True})
                self.assertEqual(_temporaries(Path(raw)), [])

    def test_declined_commit_guard_discards_the_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "store.json"
            target.write_text('{"old": true}', encoding="utf-8")
            published = write_atomically(target, lambda handle: handle.write("{}"), commit_guard=lambda: _guard(False))
            self.assertFalse(published)
            self.assertEqual(json.loads(target.read_text()), {"old": True})
            self.assertEqual(_temporaries(Path(raw)), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_existing_mode_is_preserved_and_new_files_use_the_umask_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "store.json"
            write_atomically(target, lambda handle: handle.write("{}"))
            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o666 & ~umask)
            for mode in (0o640, 0o600, 0o660):
                os.chmod(target, mode)
                write_atomically(target, lambda handle: handle.write("[]"))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)


class ConcurrentStoreWriterTests(unittest.TestCase):
    """Separate store instances share no lock, as separate admin requests do."""

    def _hammer(self, writers) -> list[BaseException]:
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(writers))

        def run(write) -> None:
            for iteration in range(ITERATIONS):
                try:
                    barrier.wait(timeout=30)
                    write(iteration)
                except threading.BrokenBarrierError:
                    return
                except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                    errors.append(exc)
                    barrier.abort()
                    return

        threads = [threading.Thread(target=run, args=(write,)) for write in writers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        return errors

    def test_two_slot_detail_stores_on_one_path_never_fail_or_publish_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "slot_detail_cache.json"
            stores = [SlotDetailStore(str(path)) for _ in range(2)]

            def writer(index: int):
                def write(iteration: int) -> None:
                    stores[index].save_entries([SlotDetailCacheEntry(
                        system_id=f"synthetic-{index}", slot=iteration % 8,
                        identifiers=[f"disk-{index}-{iteration}"],
                    )])
                    json.loads(path.read_text(encoding="utf-8"))
                return write

            errors = self._hammer([writer(0), writer(1)])
            self.assertEqual(errors, [])
            self.assertIn("slot_details", json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(_temporaries(Path(raw)), [])

    def test_two_sas_alias_stores_on_one_path_never_fail_or_publish_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sas_fabric_aliases.json"
            stores = [SasFabricAliasStore(path) for _ in range(2)]

            def writer(index: int):
                def write(iteration: int) -> None:
                    stores[index].save_alias(SasFabricAlias(
                        system_id=f"synthetic-{index}", object_id=f"enclosure-{iteration % 4}",
                        object_kind="enclosure", label=f"Shelf {iteration}",
                    ))
                    json.loads(path.read_text(encoding="utf-8"))
                return write

            # Patch once outside the threads; concurrent patch/unpatch of one
            # attribute is not thread-safe and could leak the replacement.
            with patch("app.services.sas_fabric_alias_store.record_config_change"):
                errors = self._hammer([writer(0), writer(1)])
            self.assertEqual(errors, [])
            self.assertIn("sas_fabric_aliases", json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(_temporaries(Path(raw)), [])


class SlotDetailCommitGuardTests(unittest.TestCase):
    def test_declined_commit_guard_keeps_the_file_and_leaves_no_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "slot_detail_cache.json"
            store = SlotDetailStore(str(path))
            store.save_entries([SlotDetailCacheEntry(system_id="synthetic", slot=0, identifiers=["disk-a"])])
            before = path.read_bytes()
            store.save_entries(
                [SlotDetailCacheEntry(system_id="synthetic", slot=1, identifiers=["disk-b"])],
                commit_guard=lambda: _guard(False),
            )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_temporaries(Path(raw)), [])
            store.save_entries(
                [SlotDetailCacheEntry(system_id="synthetic", slot=1, identifiers=["disk-b"])],
                commit_guard=lambda: _guard(True),
            )
            self.assertEqual(len(store.load_all()), 2)
            self.assertEqual(_temporaries(Path(raw)), [])


class TemporaryNameIgnoreRuleTests(unittest.TestCase):
    def test_unique_temporaries_are_never_listed_as_ssh_keys(self) -> None:
        for name in ("id_ed25519", "id_ed25519.pub", "slot_detail_cache.json"):
            with self.subTest(name=name):
                candidate = temporary_path_for(Path("/synthetic") / name)
                self.assertFalse(SSHKeyManager._is_private_key_candidate(candidate))

    def test_unique_temporaries_count_as_stale_nonroot_replacement_artifacts(self) -> None:
        from scripts import prepare_nonroot_bind_mounts as prepare

        with tempfile.TemporaryDirectory() as raw:
            stray = temporary_path_for(Path(raw) / "slot_detail_cache.json")
            stray.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stale replacement artifact"):
                prepare._validate_entry(stray, os.lstat(stray))

    def test_upgrade_notice_publication_does_not_use_the_fixed_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            path = upgrade_notice.state_path(data_dir)
            sibling = path.with_suffix(".tmp")
            sibling.write_text("unowned", encoding="utf-8")
            upgrade_notice.current_notice(data_dir, version="0.23.0")
            self.assertEqual(sibling.read_text(encoding="utf-8"), "unowned")
            self.assertEqual([p for p in _temporaries(data_dir) if p != sibling], [])


if __name__ == "__main__":
    unittest.main()
