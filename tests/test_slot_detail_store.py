from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import PathConfig, Settings, SystemConfig, TrueNASConfig
from app.services.inventory_registry import InventoryRegistry
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore


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
