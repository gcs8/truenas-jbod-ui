from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.models.domain import ManualMapping
from app.services.mapping_store import MappingStore


class MappingStoreImportTests(unittest.TestCase):
    def make_store(self, root: str) -> MappingStore:
        return MappingStore(str(Path(root) / "mappings.json"))

    def test_preloaded_entries_bound_load_and_validation_for_large_slot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            slot_count = 347
            store._write(
                {
                    store._slot_key("system-a", "enc-a", slot): ManualMapping(
                        system_id="system-a",
                        enclosure_id="enc-a",
                        slot=slot,
                        serial=f"SER-{slot}",
                    )
                    for slot in range(slot_count)
                }
            )
            store.load_all = MagicMock(wraps=store.load_all)  # type: ignore[method-assign]

            with patch.object(ManualMapping, "model_validate", wraps=ManualMapping.model_validate) as validate:
                loaded_entries = store.load_all()
                resolved = [
                    store.get_mapping(
                        "system-a",
                        "enc-a",
                        slot,
                        loaded_entries=loaded_entries,
                    )
                    for slot in range(slot_count)
                ]

            self.assertEqual(store.load_all.call_count, 1)
            self.assertEqual(validate.call_count, slot_count)
            self.assertEqual([mapping.slot for mapping in resolved if mapping is not None], list(range(slot_count)))

    def test_get_mapping_uses_supplied_empty_mapping_without_reloading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.load_all = MagicMock(wraps=store.load_all)  # type: ignore[method-assign]

            mapping = store.get_mapping("system-a", "enc-a", 0, loaded_entries={})

            self.assertIsNone(mapping)
            store.load_all.assert_not_called()

    def test_preloaded_entries_preserve_exact_and_legacy_fallback_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            exact = ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=3, serial="EXACT")
            fallback = ManualMapping(system_id="system-a", enclosure_id=None, slot=3, serial="FALLBACK")
            loaded_entries = {
                store._slot_key("system-a", "enc-a", 3): exact,
                store._slot_key("system-a", None, 3): fallback,
            }
            store.load_all = MagicMock(side_effect=AssertionError("preloaded lookup must not reload"))  # type: ignore[method-assign]

            self.assertIs(store.get_mapping("system-a", "enc-a", 3, loaded_entries=loaded_entries), exact)
            self.assertIsNone(store.get_mapping("system-a", "enc-b", 3, loaded_entries=loaded_entries))
            self.assertIs(
                store.get_mapping(
                    "system-a",
                    "enc-b",
                    3,
                    allow_legacy_fallback=True,
                    loaded_entries=loaded_entries,
                ),
                fallback,
            )
            self.assertIsNone(
                store.get_mapping(
                    "system-b",
                    "enc-b",
                    3,
                    allow_legacy_fallback=True,
                    loaded_entries=loaded_entries,
                )
            )
            store.load_all.assert_not_called()

    def test_preview_is_deterministic_and_classifies_semantic_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="SAME"))
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1, serial="OLD"))

            incoming = [
                ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="SAME", source="import"),
                ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1, serial="NEW", source="import"),
                ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=2, serial="ADD", source="import"),
            ]
            first = store.preview_replace_mappings("system-a", "enc-a", incoming)
            second = store.preview_replace_mappings("system-a", "enc-a", list(reversed(incoming)))

            self.assertEqual(first, second)
            self.assertEqual(
                [(item["enclosure_id"], item["slot"]) for item in first["additions"]],
                [("enc-a", 2)],
            )
            self.assertEqual(
                [(item["enclosure_id"], item["slot"]) for item in first["updates"]],
                [("enc-a", 1)],
            )
            self.assertEqual(first["removals"], [])
            self.assertEqual(first["unchanged"], [{"enclosure_id": "enc-a", "slot": 0}])
            self.assertEqual(first["additions"][0]["incoming"]["serial"], "ADD")
            self.assertEqual(
                first["updates"][0]["changes"]["serial"],
                {"from": "OLD", "to": "NEW"},
            )
            self.assertEqual(len(first["revision"]), 64)
            self.assertEqual(len(first["import_digest"]), 64)

    def test_empty_enclosure_import_removes_only_that_enclosure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id=None, slot=9, serial="FALLBACK"))
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="REMOVE"))
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-b", slot=0, serial="KEEP"))

            preview = store.preview_replace_mappings("system-a", "enc-a", [])
            self.assertEqual(preview["removals"][0]["current"]["serial"], "REMOVE")
            result = store.apply_mapping_import(
                "system-a",
                "enc-a",
                [],
                expected_revision=preview["revision"],
                import_digest=preview["import_digest"],
            )

            self.assertEqual(result["saved_count"], 0)
            self.assertIsNone(store.get_mapping("system-a", "enc-a", 0))
            self.assertEqual(store.get_mapping("system-a", None, 9).serial, "FALLBACK")
            self.assertEqual(store.get_mapping("system-a", "enc-b", 0).serial, "KEEP")

    def test_enclosure_export_scope_excludes_system_fallback_and_other_enclosures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id=None, slot=9, serial="FALLBACK"))
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="EXPORT"))
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-b", slot=0, serial="OTHER"))

            exported = store.list_mappings("system-a", "enc-a")

            self.assertEqual([(item.enclosure_id, item.slot, item.serial) for item in exported], [
                ("enc-a", 0, "EXPORT"),
            ])

    def test_stale_scope_revision_rejects_import_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="ORIGINAL"))
            incoming = [ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="IMPORT")]
            preview = store.preview_replace_mappings("system-a", "enc-a", incoming)

            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1, serial="NEWER"))

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.apply_mapping_import(
                    "system-a",
                    "enc-a",
                    incoming,
                    expected_revision=preview["revision"],
                    import_digest=preview["import_digest"],
                )

            self.assertEqual(store.get_mapping("system-a", "enc-a", 0).serial, "ORIGINAL")
            self.assertEqual(store.get_mapping("system-a", "enc-a", 1).serial, "NEWER")

    def test_scope_revision_binds_system_and_enclosure_even_when_both_scopes_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)

            enc_a = store.scope_revision("system-a", "enc-a")
            enc_b = store.scope_revision("system-a", "enc-b")
            other_system = store.scope_revision("system-b", "enc-a")

            self.assertNotEqual(enc_a, enc_b)
            self.assertNotEqual(enc_a, other_system)

    def test_single_mapping_save_and_clear_require_current_scope_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            empty_revision = store.preview_replace_mappings("system-a", "enc-a", [])["revision"]
            self.assertEqual(store.scope_revision("system-a", "enc-a"), empty_revision)
            saved = store.save_mapping(
                ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="FIRST"),
                expected_revision=empty_revision,
            )
            current_revision = store.preview_replace_mappings("system-a", "enc-a", [saved])["revision"]
            self.assertEqual(store.scope_revision("system-a", "enc-a"), current_revision)
            clear_revision = store.clear_revision("system-a", "enc-a", 0)

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.save_mapping(
                    ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1, serial="STALE"),
                    expected_revision=empty_revision,
                )
            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.clear_mapping(
                    "system-a",
                    "enc-a",
                    0,
                    expected_revision=empty_revision,
                )

            self.assertEqual(store.get_mapping("system-a", "enc-a", 0).serial, "FIRST")
            self.assertIsNone(store.get_mapping("system-a", "enc-a", 1))
            self.assertTrue(store.clear_mapping(
                "system-a",
                "enc-a",
                0,
                expected_revision=clear_revision,
            ))

    def test_fallback_clear_rejects_concurrent_change_elsewhere_in_exact_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.save_mapping(
                ManualMapping(system_id="system-a", enclosure_id=None, slot=0, serial="FALLBACK")
            )
            clear_revision = store.clear_revision("system-a", "enc-a", 0)
            store.save_mapping(
                ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1, serial="NEWER")
            )

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.clear_mapping(
                    "system-a",
                    "enc-a",
                    0,
                    expected_revision=clear_revision,
                )

            self.assertEqual(store.get_mapping("system-a", None, 0).serial, "FALLBACK")
            self.assertEqual(store.get_mapping("system-a", "enc-a", 1).serial, "NEWER")

    def test_legacy_fallback_values_participate_in_clear_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            legacy = ManualMapping(
                system_id="system-a",
                enclosure_id=None,
                slot=0,
                serial="LEGACY-OLD",
            )
            store._write({"default:0": legacy})
            clear_revision = store.clear_revision("system-a", "enc-a", 0)
            store._write({"default:0": legacy.model_copy(update={"serial": "LEGACY-NEW"})})

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.clear_mapping(
                    "system-a",
                    "enc-a",
                    0,
                    expected_revision=clear_revision,
                )

            current = store.get_mapping(
                "system-a",
                None,
                0,
                allow_legacy_fallback=True,
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.serial, "LEGACY-NEW")

    def test_authentic_legacy_row_is_reported_and_removed_by_confirmed_empty_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            legacy = ManualMapping(
                system_id=None,
                enclosure_id="enc-a",
                slot=0,
                serial="LEGACY-ORIGINAL",
            )
            store._write({"enc-a:0": legacy})

            preview = store.preview_replace_mappings("system-a", "enc-a", [])

            self.assertEqual(
                preview["removals"],
                [{
                    "enclosure_id": "enc-a",
                    "slot": 0,
                    "current": {
                        "serial": "LEGACY-ORIGINAL",
                        "device_name": None,
                        "gptid": None,
                        "notes": None,
                    },
                }],
            )
            store.apply_mapping_import(
                "system-a",
                "enc-a",
                [],
                expected_revision=preview["revision"],
                import_digest=preview["import_digest"],
            )
            self.assertIsNone(store.get_mapping("system-a", "enc-a", 0))

    def test_authentic_legacy_fallback_change_invalidates_clear_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            legacy = ManualMapping(
                system_id=None,
                enclosure_id=None,
                slot=0,
                serial="LEGACY-OLD",
            )
            store._write({"default:0": legacy})
            clear_revision = store.clear_revision("system-a", "enc-a", 0)
            store._write({"default:0": legacy.model_copy(update={"serial": "LEGACY-NEW"})})

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.clear_mapping(
                    "system-a",
                    "enc-a",
                    0,
                    expected_revision=clear_revision,
                )

            current = store.get_mapping(
                "system-a",
                "enc-a",
                0,
                allow_legacy_fallback=True,
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.serial, "LEGACY-NEW")

    def test_authentic_legacy_row_is_visible_to_system_scoped_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            legacy = ManualMapping(
                system_id=None,
                enclosure_id="enc-a",
                slot=0,
                serial="LEGACY",
            )
            store._write({"enc-a:0": legacy})

            self.assertEqual(store.count_for_system("system-a"), 1)
            self.assertEqual(
                [mapping.serial for mapping in store.list_mappings("system-a", "enc-a")],
                ["LEGACY"],
            )

    def test_unscoped_legacy_mapping_requires_single_scope_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            legacy = ManualMapping(
                system_id=None,
                enclosure_id=None,
                slot=3,
                device_name="sda",
            )
            store._write({"default:3": legacy})

            self.assertIsNone(store.get_mapping("system-b", "enc-b", 3))
            admitted = store.get_mapping(
                "system-b",
                "enc-b",
                3,
                allow_legacy_fallback=True,
            )
            self.assertIsNotNone(admitted)
            assert admitted is not None
            self.assertEqual(admitted.device_name, "sda")

    def test_enclosureless_mapping_requires_single_scope_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.save_mapping(
                ManualMapping(
                    system_id="system-a",
                    enclosure_id=None,
                    slot=3,
                    serial="SYSTEM-FALLBACK",
                )
            )

            self.assertIsNone(store.get_mapping("system-a", "enc-b", 3))
            admitted = store.get_mapping(
                "system-a",
                "enc-b",
                3,
                allow_legacy_fallback=True,
            )
            self.assertIsNotNone(admitted)
            assert admitted is not None
            self.assertEqual(admitted.serial, "SYSTEM-FALLBACK")

    def test_exact_key_rejects_mapping_owned_by_another_system(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store._write({
                store._slot_key("system-b", "enc-b", 3): ManualMapping(
                    system_id="system-a",
                    enclosure_id="enc-b",
                    slot=3,
                    serial="WRONG-SYSTEM",
                )
            })

            self.assertIsNone(store.get_mapping("system-b", "enc-b", 3))

    def test_colon_bearing_legacy_enclosure_key_remains_unscoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            legacy = ManualMapping(
                system_id=None,
                enclosure_id="sas:enclosure-a",
                slot=0,
                serial="LEGACY-COLON",
            )
            store._write({"sas:enclosure-a:0": legacy})

            self.assertEqual(
                [mapping.serial for mapping in store.list_mappings("system-a", "sas:enclosure-a")],
                ["LEGACY-COLON"],
            )

    def test_direct_system_replacement_removes_authentic_legacy_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            legacy = ManualMapping(
                system_id=None,
                enclosure_id="enc-a",
                slot=0,
                serial="LEGACY",
            )
            store._write({"enc-a:0": legacy})

            store.replace_mappings("system-a", "enc-a", [])

            self.assertIsNone(store.get_mapping("system-a", "enc-a", 0))

    def test_canonical_save_removes_matching_legacy_alias(self) -> None:
        for enclosure_id in ("enc-a", "sas:enclosure-a"):
            with self.subTest(enclosure_id=enclosure_id), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                legacy_key = f"{enclosure_id}:0"
                store._write({
                    legacy_key: ManualMapping(
                        system_id=None,
                        enclosure_id=enclosure_id,
                        slot=0,
                        serial="LEGACY",
                    )
                })
                revision = store.scope_revision("system-a", enclosure_id)

                store.save_mapping(
                    ManualMapping(
                        system_id="system-a",
                        enclosure_id=enclosure_id,
                        slot=0,
                        serial="CANONICAL",
                    ),
                    expected_revision=revision,
                )

                current = store.load_all()
                self.assertNotIn(legacy_key, current)
                self.assertEqual(
                    list(current),
                    [store._slot_key("system-a", enclosure_id, 0)],
                )

    def test_canonical_save_removes_global_legacy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store._write({
                "default:0": ManualMapping(
                    system_id=None,
                    enclosure_id=None,
                    slot=0,
                    serial="LEGACY",
                )
            })

            store.save_mapping(
                ManualMapping(
                    system_id="system-a",
                    enclosure_id="enc-a",
                    slot=0,
                    serial="CANONICAL",
                )
            )

            current = store.load_all()
            self.assertNotIn("default:0", current)
            self.assertEqual(
                list(current),
                [store._slot_key("system-a", "enc-a", 0)],
            )

    def test_preexisting_legacy_alias_collapses_to_canonical_effective_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            canonical = ManualMapping(
                system_id="default",
                enclosure_id="enc-a",
                slot=0,
                serial="CANONICAL",
            )
            legacy = canonical.model_copy(update={"system_id": None, "serial": "LEGACY"})
            store._write({
                store._slot_key("default", "enc-a", 0): canonical,
                "enc-a:0": legacy,
            })

            exported = store.list_mappings("default", "enc-a")
            preview = store.preview_replace_mappings("default", "enc-a", exported)

            self.assertEqual(store.count_for_system("default"), 1)
            self.assertEqual([mapping.serial for mapping in exported], ["CANONICAL"])
            self.assertEqual(preview["additions"], [])
            self.assertEqual(preview["updates"], [])
            self.assertEqual(preview["removals"], [])
            self.assertEqual(len(preview["unchanged"]), 1)

    def test_canonical_save_invalidates_pre_migration_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store._write({
                "enc-a:0": ManualMapping(
                    system_id=None,
                    enclosure_id="enc-a",
                    slot=0,
                    serial="LEGACY",
                )
            })
            stale_revision = store.scope_revision("default", "enc-a")
            store.save_mapping(
                ManualMapping(
                    system_id="default",
                    enclosure_id="enc-a",
                    slot=0,
                    serial="CANONICAL-1",
                ),
                expected_revision=stale_revision,
            )

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.save_mapping(
                    ManualMapping(
                        system_id="default",
                        enclosure_id="enc-a",
                        slot=0,
                        serial="CANONICAL-2",
                    ),
                    expected_revision=stale_revision,
                )

    def test_clear_removes_canonical_mapping_and_matching_legacy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            canonical = ManualMapping(
                system_id="default",
                enclosure_id="enc-a",
                slot=0,
                serial="CANONICAL",
            )
            legacy = canonical.model_copy(update={"system_id": None, "serial": "LEGACY"})
            store._write({
                store._slot_key("default", "enc-a", 0): canonical,
                "enc-a:0": legacy,
            })
            revision = store.clear_revision("default", "enc-a", 0)

            self.assertTrue(
                store.clear_mapping(
                    "default",
                    "enc-a",
                    0,
                    expected_revision=revision,
                )
            )
            self.assertIsNone(store.get_mapping("default", "enc-a", 0))
            self.assertEqual(store.load_all(), {})

    def test_canonical_save_removes_scoped_enclosureless_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store._write({
                store._slot_key("system-a", None, 5): ManualMapping(
                    system_id="system-a",
                    enclosure_id=None,
                    slot=5,
                    serial="SYNTH-OLD-A",
                )
            })

            store.save_mapping(
                ManualMapping(
                    system_id="system-a",
                    enclosure_id="enc-a",
                    slot=5,
                    serial="SYNTH-NEW-B",
                )
            )

            current = store.load_all()
            self.assertNotIn(store._slot_key("system-a", None, 5), current)
            self.assertEqual(list(current), [store._slot_key("system-a", "enc-a", 5)])

    def test_clear_removes_scoped_enclosureless_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store._write({
                store._slot_key("system-a", None, 5): ManualMapping(
                    system_id="system-a",
                    enclosure_id=None,
                    slot=5,
                    serial="SYNTH-OLD-A",
                ),
                store._slot_key("system-a", "enc-a", 5): ManualMapping(
                    system_id="system-a",
                    enclosure_id="enc-a",
                    slot=5,
                    serial="SYNTH-NEW-B",
                ),
            })

            self.assertTrue(store.clear_mapping("system-a", "enc-a", 5))

            self.assertIsNone(
                store.get_mapping(
                    "system-a",
                    "enc-a",
                    5,
                    allow_legacy_fallback=True,
                )
            )
            self.assertEqual(store.load_all(), {})

    def test_default_system_clear_keeps_other_system_canonical_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            default_mapping = ManualMapping(
                system_id=None,
                enclosure_id="enc-a",
                slot=0,
                serial="DEFAULT",
            )
            other_mapping = default_mapping.model_copy(
                update={"system_id": "system-a", "serial": "SYSTEM-A"}
            )
            store._write({
                store._slot_key(None, "enc-a", 0): default_mapping,
                store._slot_key("system-a", "enc-a", 0): other_mapping,
            })

            self.assertTrue(store.clear_mapping(None, "enc-a", 0))
            self.assertIsNone(store.get_mapping(None, "enc-a", 0))
            remaining = store.get_mapping("system-a", "enc-a", 0)
            self.assertIsNotNone(remaining)
            assert remaining is not None
            self.assertEqual(remaining.serial, "SYSTEM-A")

    def test_preview_digest_binds_exact_incoming_mapping_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            original = [ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="FIRST")]
            preview = store.preview_replace_mappings("system-a", "enc-a", original)
            changed = [ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="SECOND")]

            with self.assertRaisesRegex(RuntimeError, "digest"):
                store.apply_mapping_import(
                    "system-a",
                    "enc-a",
                    changed,
                    expected_revision=preview["revision"],
                    import_digest=preview["import_digest"],
                )

            self.assertIsNone(store.get_mapping("system-a", "enc-a", 0))


if __name__ == "__main__":
    unittest.main()
