from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.config import PathConfig, Settings, SystemConfig, TrueNASConfig
from app.services.inventory_registry import InventoryRegistry
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore


class SlotDetailStoreBatchConflictTests(unittest.TestCase):
    def test_batch_reloads_under_lock_and_does_not_overwrite_conflicting_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotDetailStore(str(Path(directory) / "details.json"))
            first = SlotDetailCacheEntry(system_id="invented-a", slot=0, identifiers=["disk-a"])
            second = first.model_copy(update={"slot": 1})
            store.save_entries([first, second])
            baseline = store.load_all()
            replacement = first.model_copy(update={"identifiers": ["disk-replaced"]})
            other = first.model_copy(update={"system_id": "invented-b"})
            # Real competing write after the batch's read snapshot.
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(store.save_entries, [replacement, other]).result(timeout=5)
            changed = second.model_copy(update={"updated_at": "2030-01-01T00:00:00+00:00"})
            with patch.object(store, "load_all", wraps=store.load_all) as load:
                store.save_entries([first, changed], expected_entries=baseline)
                self.assertEqual(load.call_count, 1)
            self.assertEqual(store.get_entry("invented-a", None, 0), replacement)
            self.assertEqual(store.get_entry("invented-a", None, 1), changed)
            self.assertEqual(store.get_entry("invented-b", None, 0), other)


class SlotDetailStoreSaveTests(unittest.TestCase):
    @staticmethod
    def _entry(**updates) -> SlotDetailCacheEntry:
        fields = dict(
            system_id="system-a", enclosure_id="enc-1", slot=0,
            identifiers=["disk-a"], slot_fields={"model": "model-a"},
            smart_fields={"temperature": 30}, updated_at="2026-01-01T00:00:00+00:00",
        )
        fields.update(updates)
        return SlotDetailCacheEntry.model_validate(fields)

    def test_save_operation_counts_and_exact_persisted_payloads(self) -> None:
        original = self._entry()
        changed = self._entry(smart_fields={"temperature": 31})
        other = self._entry(system_id="system-b")
        cases = [
            ("empty", [], 0, 0),
            ("identical", [self._entry()], 1, 0),
            ("changed", [changed], 1, 1),
            ("timestamp-only", [self._entry(updated_at="2026-01-01T00:00:01+00:00")], 1, 1),
            ("replacement", [self._entry(identifiers=["disk-b"])], 1, 1),
            ("slot-fields", [self._entry(slot_fields={"model": "model-b"})], 1, 1),
            ("duplicate-restored", [changed, self._entry()], 1, 0),
            ("duplicate-changed", [self._entry(), changed], 1, 1),
            ("multi-system", [changed, other], 1, 1),
            ("multi-system-identical", [other, self._entry()], 1, 0),
        ]
        for name, batch, loads, writes in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "slot_detail_cache.json"
                store = SlotDetailStore(str(path))
                store.save_entries([original, other])
                before = path.read_bytes()
                before_stat = path.stat()
                expected = json.loads(before)["slot_details"]
                for entry in batch:
                    expected[store._slot_key(entry.system_id, entry.enclosure_id, entry.slot)] = entry.model_dump(mode="json")
                with (
                    patch.object(store, "load_all", wraps=store.load_all) as load,
                    patch.object(store, "_write", wraps=store._write) as write,
                    patch.object(Path, "replace", autospec=True, side_effect=Path.replace) as replace,
                ):
                    store.save_entries(batch)
                self.assertEqual((load.call_count, write.call_count, replace.call_count), (loads, writes, writes))
                self.assertEqual(json.loads(path.read_bytes())["slot_details"], expected)
                if not writes:
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(path.stat().st_mtime_ns, before_stat.st_mtime_ns)
                    self.assertEqual(path.stat().st_ino, before_stat.st_ino)
                with patch.object(store, "_write", wraps=store._write) as write:
                    store.save_entries(batch)
                write.assert_not_called()

    def test_empty_save_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "slot_detail_cache.json"
            store = SlotDetailStore(str(path))
            with patch.object(store, "load_all", wraps=store.load_all) as load:
                store.save_entries([])
            load.assert_not_called()
            self.assertFalse(path.exists())

    def test_json_type_changes_are_not_python_equality_noops(self) -> None:
        for previous, following in [(True, 1), (1, 1.0)]:
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "slot_detail_cache.json"
                store = SlotDetailStore(str(path))
                store.save_entries([self._entry(smart_fields={"value": previous})])
                with patch.object(store, "_write", wraps=store._write) as write:
                    store.save_entries([self._entry(smart_fields={"value": following})])
                self.assertEqual(write.call_count, 1)
                saved = next(iter(store.load_all().values())).smart_fields["value"]
                self.assertIs(type(saved), type(following))

    def test_concurrent_system_batches_preserve_unrelated_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json"))
            store.save_entries([self._entry(system_id="untouched")])
            ready = threading.Barrier(2)

            def save(system_id):
                ready.wait(timeout=5)
                store.save_entries([self._entry(system_id=system_id)])

            with (
                patch.object(store, "load_all", wraps=store.load_all) as load,
                patch.object(store, "_write", wraps=store._write) as write,
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                futures = [pool.submit(save, system_id) for system_id in ("system-a", "system-b")]
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual((load.call_count, write.call_count), (2, 2))
            self.assertEqual(
                {entry.system_id for entry in store.load_all().values()},
                {"untouched", "system-a", "system-b"},
            )

    def test_full_identity_changes_with_same_slot_key_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json"))
            store.save_entries([self._entry(system_id=None, enclosure_id=None)])
            explicit = self._entry(system_id="default_system", enclosure_id="default")
            with patch.object(store, "_write", wraps=store._write) as write:
                store.save_entries([explicit])
            self.assertEqual(write.call_count, 1)
            self.assertEqual(list(store.load_all().values()), [explicit])

    def test_dictionary_order_does_not_change_sorted_json_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json"))
            store.save_entries([self._entry(smart_fields={"a": 1, "b": {"c": 2, "d": 3}})])
            with patch.object(store, "_write", wraps=store._write) as write:
                store.save_entries([self._entry(smart_fields={"b": {"d": 3, "c": 2}, "a": 1})])
            write.assert_not_called()

    def test_merge_load_and_write_stay_under_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json"))
            real_load, real_write = store.load_all, store._write

            def locked_load():
                self.assertTrue(store._lock.locked())
                return real_load()

            def locked_write(entries):
                self.assertTrue(store._lock.locked())
                return real_write(entries)

            with (
                patch.object(store, "load_all", side_effect=locked_load),
                patch.object(store, "_write", side_effect=locked_write) as write,
            ):
                store.save_entries([self._entry()])
                store.save_entries([self._entry(system_id="system-b")])
                store.save_entries([self._entry()])
            self.assertEqual(write.call_count, 2)
            self.assertEqual(len(store.load_all()), 2)


class SlotDetailStorePruneTests(unittest.TestCase):
    @staticmethod
    def _entry(system_id: str, slot: int) -> SlotDetailCacheEntry:
        return SlotDetailCacheEntry(
            system_id=system_id,
            enclosure_id="enc-1",
            slot=slot,
            identifiers=[f"disk-{slot}"],
            slot_fields={"model": f"model-{slot}"},
        )

    def test_prune_unknown_systems_removes_only_unknown_rows_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "slot_detail_cache.json"
            store = SlotDetailStore(str(path))
            store.save_entries(
                [
                    self._entry("old-id", 0),
                    self._entry("old-id", 1),
                    self._entry("new-id", 0),
                ]
            )

            removed = store.prune_unknown_systems({"new-id"})

            self.assertEqual(removed, 2)
            remaining = SlotDetailStore(str(path)).load_all()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(next(iter(remaining.values())).system_id, "new-id")

    def test_prune_unknown_systems_does_not_rewrite_when_every_owner_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json"))
            store.save_entries([self._entry("new-id", 0)])

            with patch.object(store, "_write", wraps=store._write) as write:
                removed = store.prune_unknown_systems({"new-id"})

            self.assertEqual(removed, 0)
            write.assert_not_called()

    def test_prune_unknown_systems_preserves_malformed_cache_bytes(self) -> None:
        malformed_payloads = [
            [],
            {"slot_details": []},
            {"slot_details": {"bad": {"system_id": "new-id", "slot": "not-an-integer"}}},
        ]
        for payload in malformed_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "slot_detail_cache.json"
                original = json.dumps(payload).encode()
                path.write_bytes(original)
                store = SlotDetailStore(str(path))

                removed = store.prune_unknown_systems({"new-id"})

                self.assertEqual(removed, 0)
                self.assertEqual(path.read_bytes(), original)

    def test_load_all_rejects_malformed_container_shapes_without_raising(self) -> None:
        malformed_payloads = [
            None,
            [],
            {"slot_details": []},
            {"slot_details": "not-a-mapping"},
        ]
        for payload in malformed_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "slot_detail_cache.json"
                path.write_text(json.dumps(payload), encoding="utf-8")

                self.assertEqual(SlotDetailStore(str(path)).load_all(), {})

    def test_load_all_drops_only_invalid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "slot_detail_cache.json"
            path.write_text(
                json.dumps(
                    {
                        "slot_details": {
                            "system-a:enc-1:0": self._entry("system-a", 0).model_dump(mode="json"),
                            "system-a:enc-1:1": {"system_id": "system-a", "slot": "not-an-integer"},
                            "system-a:enc-1:2": self._entry("system-a", 2).model_dump(mode="json"),
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = SlotDetailStore(str(path)).load_all()

            self.assertEqual(list(loaded), ["system-a:enc-1:0", "system-a:enc-1:2"])
            self.assertEqual([entry.slot for entry in loaded.values()], [0, 2])

    def test_save_entries_self_heals_malformed_content(self) -> None:
        malformed_payloads = [
            [],
            {"slot_details": []},
            {"slot_details": {"bad": {"system_id": "system-a", "slot": "bad"}}},
        ]
        for payload in malformed_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "slot_detail_cache.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                store = SlotDetailStore(str(path))

                store.save_entries([self._entry("system-a", 3)])

                loaded = SlotDetailStore(str(path)).load_all()
                self.assertEqual(list(loaded), ["system-a:enc-1:3"])
                self.assertEqual(loaded["system-a:enc-1:3"].slot, 3)

    def test_registry_prunes_unknown_system_rows_and_logs_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            slot_detail_path = root / "slot_detail_cache.json"
            store = SlotDetailStore(str(slot_detail_path))
            store.save_entries(
                [
                    self._entry("old-id", 0),
                    self._entry("old-id", 1),
                    self._entry("new-id", 0),
                ]
            )
            settings = Settings(
                systems=[SystemConfig(id="new-id", truenas=TrueNASConfig(platform="core"))],
                default_system_id="new-id",
                paths=PathConfig(
                    mapping_file=str(root / "slot_mappings.json"),
                    sas_fabric_alias_file=str(root / "sas_fabric_aliases.json"),
                    log_file=str(root / "app.log"),
                    profile_file=str(root / "profiles.yaml"),
                    slot_detail_cache_file=str(slot_detail_path),
                ),
            )

            with self.assertLogs("app.services.inventory_registry", level="INFO") as captured:
                registry = InventoryRegistry(settings)

            self.assertIsNotNone(registry)
            remaining = SlotDetailStore(str(slot_detail_path)).load_all()
            self.assertEqual([entry.system_id for entry in remaining.values()], ["new-id"])
            self.assertEqual(len(captured.records), 1)
            self.assertIn("Pruned 2 stale slot-detail cache rows", captured.records[0].getMessage())


if __name__ == "__main__":
    unittest.main()
