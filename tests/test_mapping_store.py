from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.models.domain import ManualMapping
from app.services.mapping_store import (
    MappingRevisionConflict,
    MappingScopeConflict,
    MappingStore,
    resolve_physical_mapping_scope,
)


DRAWER_TOP = "synthetic-shelf-a::dell-md1280-drawer-top-42"
DRAWER_BOTTOM = "synthetic-shelf-a::dell-md1280-drawer-bottom-42"


class PhysicalMappingScopeResolverTests(unittest.TestCase):
    def test_resolver_normalizes_only_exact_recognized_drawer_suffixes(self) -> None:
        matrix = {
            None: None,
            "synthetic-shelf-a": "synthetic-shelf-a",
            DRAWER_TOP: "synthetic-shelf-a",
            DRAWER_BOTTOM: "synthetic-shelf-a",
            "synthetic-shelf-a::unknown-drawer": "synthetic-shelf-a::unknown-drawer",
            "::dell-md1280-drawer-top-42": "::dell-md1280-drawer-top-42",
            "synthetic-shelf-a::": "synthetic-shelf-a::",
            " synthetic-shelf-a::dell-md1280-drawer-top-42 ": (
                " synthetic-shelf-a::dell-md1280-drawer-top-42 "
            ),
            "synthetic-shelf-a::DELL-MD1280-DRAWER-TOP-42": (
                "synthetic-shelf-a::DELL-MD1280-DRAWER-TOP-42"
            ),
            "synthetic-shelf-a::label-top": "synthetic-shelf-a::label-top",
            "synthetic-shelf-a::41": "synthetic-shelf-a::41",
            "synthetic-shelf-a::unknown::dell-md1280-drawer-top-42": (
                "synthetic-shelf-a::unknown"
            ),
            (
                "synthetic-shelf-a::dell-md1280-drawer-top-42"
                "::dell-md1280-drawer-bottom-42"
            ): (
                "synthetic-shelf-a::dell-md1280-drawer-top-42"
                "::dell-md1280-drawer-bottom-42"
            ),
        }

        for enclosure_id, expected in matrix.items():
            with self.subTest(enclosure_id=enclosure_id):
                resolved = resolve_physical_mapping_scope(enclosure_id)
                self.assertEqual(resolved, expected)
                self.assertEqual(resolve_physical_mapping_scope(resolved), expected)


class PhysicalMappingScopeLifecycleTests(unittest.TestCase):
    def make_store(self, root: str) -> MappingStore:
        return MappingStore(str(Path(root) / "mappings.json"))

    @staticmethod
    def alias_key(system_id: str, enclosure_id: str, slot: int) -> str:
        return f"{system_id}:{enclosure_id}:{slot}"

    def test_drawer_requests_share_one_canonical_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            mapping = ManualMapping(
                system_id="synthetic-system-a",
                enclosure_id=DRAWER_TOP,
                slot=7,
                serial="SYNTHETIC-7",
            )
            empty_revision = store.save_revision("synthetic-system-a", DRAWER_TOP, 7)

            saved = store.save_mapping(mapping, expected_revision=empty_revision)

            self.assertEqual(saved.enclosure_id, "synthetic-shelf-a")
            self.assertEqual(
                store.save_revision("synthetic-system-a", DRAWER_BOTTOM, 7),
                store.save_revision("synthetic-system-a", "synthetic-shelf-a", 7),
            )
            self.assertEqual(
                store.clear_revision("synthetic-system-a", DRAWER_TOP, 7),
                store.clear_revision("synthetic-system-a", DRAWER_BOTTOM, 7),
            )
            for scope in ("synthetic-shelf-a", DRAWER_TOP, DRAWER_BOTTOM):
                with self.subTest(scope=scope):
                    resolved = store.get_mapping("synthetic-system-a", scope, 7)
                    self.assertIsNotNone(resolved)
                    assert resolved is not None
                    self.assertEqual(resolved.enclosure_id, "synthetic-shelf-a")
                    self.assertEqual(
                        [(item.enclosure_id, item.slot) for item in store.list_mappings(
                            "synthetic-system-a", scope
                        )],
                        [("synthetic-shelf-a", 7)],
                    )
                    self.assertEqual(
                        store.scope_revision("synthetic-system-a", scope),
                        store.scope_revision("synthetic-system-a", "synthetic-shelf-a"),
                    )
            self.assertEqual(
                list(store.load_all()),
                [store._slot_key("synthetic-system-a", "synthetic-shelf-a", 7)],
            )

            incoming = [mapping.model_copy(update={"enclosure_id": DRAWER_BOTTOM, "serial": "IMPORTED"})]
            preview = store.preview_replace_mappings(
                "synthetic-system-a", DRAWER_BOTTOM, incoming
            )
            self.assertEqual(preview["updates"][0]["enclosure_id"], "synthetic-shelf-a")
            result = store.apply_mapping_import(
                "synthetic-system-a",
                DRAWER_TOP,
                incoming,
                expected_revision=preview["revision"],
                import_digest=preview["import_digest"],
            )
            self.assertEqual(result["saved_count"], 1)
            self.assertEqual(
                store.get_mapping("synthetic-system-a", "synthetic-shelf-a", 7).serial,
                "IMPORTED",
            )

    def test_historical_drawer_alias_reads_without_writing_then_collapses_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            alias = ManualMapping(
                system_id="synthetic-system-a",
                enclosure_id=DRAWER_TOP,
                slot=7,
                serial="ALIAS",
            )
            alias_key = self.alias_key("synthetic-system-a", DRAWER_TOP, 7)
            store._write({alias_key: alias})
            before = store.file_path.read_bytes()

            resolved = store.get_mapping("synthetic-system-a", DRAWER_BOTTOM, 7)
            listed = store.list_mappings("synthetic-system-a", "synthetic-shelf-a")
            preview = store.preview_replace_mappings("synthetic-system-a", DRAWER_TOP, listed)

            self.assertEqual(store.file_path.read_bytes(), before)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.enclosure_id, "synthetic-shelf-a")
            self.assertEqual(listed[0].enclosure_id, "synthetic-shelf-a")
            self.assertEqual(preview["unchanged"], [{"enclosure_id": "synthetic-shelf-a", "slot": 7}])

            store.save_mapping(
                alias.model_copy(update={"enclosure_id": DRAWER_BOTTOM}),
                expected_revision=store.save_revision("synthetic-system-a", DRAWER_TOP, 7),
            )
            current = store.load_all()
            self.assertNotIn(alias_key, current)
            self.assertEqual(
                list(current),
                [store._slot_key("synthetic-system-a", "synthetic-shelf-a", 7)],
            )
            self.assertEqual(next(iter(current.values())).enclosure_id, "synthetic-shelf-a")

    def test_confirmed_clear_and_import_collapse_historical_drawer_aliases(self) -> None:
        for operation in ("clear", "import"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                alias = ManualMapping(
                    system_id="synthetic-system-a",
                    enclosure_id=DRAWER_TOP,
                    slot=7,
                    serial="ALIAS",
                )
                alias_key = self.alias_key("synthetic-system-a", DRAWER_TOP, 7)
                store._write({alias_key: alias})

                if operation == "clear":
                    revision = store.clear_revision(
                        "synthetic-system-a", DRAWER_BOTTOM, 7
                    )
                    self.assertTrue(store.clear_mapping(
                        "synthetic-system-a",
                        DRAWER_BOTTOM,
                        7,
                        expected_revision=revision,
                    ))
                    self.assertEqual(store.load_all(), {})
                else:
                    incoming = [alias.model_copy(update={"enclosure_id": DRAWER_BOTTOM})]
                    preview = store.preview_replace_mappings(
                        "synthetic-system-a", DRAWER_BOTTOM, incoming
                    )
                    store.apply_mapping_import(
                        "synthetic-system-a",
                        DRAWER_BOTTOM,
                        incoming,
                        expected_revision=preview["revision"],
                        import_digest=preview["import_digest"],
                    )
                    current = store.load_all()
                    self.assertNotIn(alias_key, current)
                    self.assertEqual(
                        list(current),
                        [store._slot_key(
                            "synthetic-system-a",
                            "synthetic-shelf-a",
                            7,
                        )],
                    )

    def test_replace_canonicalizes_requested_scope_and_incoming_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)

            saved_count = store.replace_mappings(
                "synthetic-system-a",
                DRAWER_TOP,
                [ManualMapping(
                    system_id="other-system",
                    enclosure_id=DRAWER_BOTTOM,
                    slot=7,
                    serial="REPLACED",
                )],
            )

            self.assertEqual(saved_count, 1)
            current = store.load_all()
            self.assertEqual(
                list(current),
                [store._slot_key("synthetic-system-a", "synthetic-shelf-a", 7)],
            )
            mapping = next(iter(current.values()))
            self.assertEqual(mapping.system_id, "synthetic-system-a")
            self.assertEqual(mapping.enclosure_id, "synthetic-shelf-a")

    def test_unfiltered_list_collapses_nonconflicting_drawer_aliases_per_system(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            canonical = ManualMapping(
                system_id="synthetic-system-a",
                enclosure_id="synthetic-shelf-a",
                slot=7,
                serial="SAME",
            )
            alias = canonical.model_copy(update={"enclosure_id": DRAWER_TOP})
            other = canonical.model_copy(
                update={
                    "system_id": "synthetic-system-b",
                    "enclosure_id": DRAWER_BOTTOM,
                }
            )
            store._write({
                store._slot_key("synthetic-system-a", "synthetic-shelf-a", 7): canonical,
                self.alias_key("synthetic-system-a", DRAWER_TOP, 7): alias,
                self.alias_key("synthetic-system-b", DRAWER_BOTTOM, 7): other,
            })

            listed = store.list_mappings()

            self.assertEqual(
                [(item.system_id, item.enclosure_id, item.slot) for item in listed],
                [
                    ("synthetic-system-a", "synthetic-shelf-a", 7),
                    ("synthetic-system-b", "synthetic-shelf-a", 7),
                ],
            )

    def test_alias_sibling_changes_invalidate_save_clear_and_import_tokens(self) -> None:
        operations = ("save", "clear", "import")
        for operation in operations:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                alias_key = self.alias_key("synthetic-system-a", DRAWER_TOP, 7)
                alias = ManualMapping(
                    system_id="synthetic-system-a",
                    enclosure_id=DRAWER_TOP,
                    slot=7,
                    serial="OLD",
                )
                store._write({alias_key: alias})
                save_revision = store.save_revision("synthetic-system-a", DRAWER_BOTTOM, 7)
                clear_revision = store.clear_revision("synthetic-system-a", DRAWER_BOTTOM, 7)
                incoming = [alias.model_copy(update={"enclosure_id": DRAWER_BOTTOM, "serial": "NEW"})]
                preview = store.preview_replace_mappings(
                    "synthetic-system-a", DRAWER_BOTTOM, incoming
                )
                store._write({alias_key: alias.model_copy(update={"notes": "changed"})})
                before = store.file_path.read_bytes()

                with self.assertRaises(MappingRevisionConflict):
                    if operation == "save":
                        store.save_mapping(incoming[0], expected_revision=save_revision)
                    elif operation == "clear":
                        store.clear_mapping(
                            "synthetic-system-a",
                            DRAWER_BOTTOM,
                            7,
                            expected_revision=clear_revision,
                        )
                    else:
                        store.apply_mapping_import(
                            "synthetic-system-a",
                            DRAWER_BOTTOM,
                            incoming,
                            expected_revision=preview["revision"],
                            import_digest=preview["import_digest"],
                        )
                self.assertEqual(store.file_path.read_bytes(), before)

    def test_divergent_canonical_and_drawer_aliases_fail_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            canonical = ManualMapping(
                system_id="synthetic-system-a",
                enclosure_id="synthetic-shelf-a",
                slot=7,
                serial="CANONICAL",
            )
            alias = canonical.model_copy(update={"enclosure_id": DRAWER_TOP, "serial": "DIVERGENT"})
            store._write({
                store._slot_key("synthetic-system-a", "synthetic-shelf-a", 7): canonical,
                self.alias_key("synthetic-system-a", DRAWER_TOP, 7): alias,
            })
            before = store.file_path.read_bytes()

            calls = (
                lambda: store.get_mapping("synthetic-system-a", DRAWER_BOTTOM, 7),
                lambda: store.list_mappings("synthetic-system-a", DRAWER_TOP),
                lambda: store.preview_replace_mappings("synthetic-system-a", DRAWER_TOP, []),
                lambda: store.save_mapping(canonical.model_copy(update={"serial": "NEW"})),
                lambda: store.clear_mapping("synthetic-system-a", DRAWER_TOP, 7),
                lambda: store.replace_mappings("synthetic-system-a", DRAWER_BOTTOM, []),
            )
            for call in calls:
                with self.subTest(call=call), self.assertRaises(MappingScopeConflict):
                    call()
                self.assertEqual(store.file_path.read_bytes(), before)

    def test_divergent_admitted_legacy_drawer_alias_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            canonical = ManualMapping(
                system_id="synthetic-system-a",
                enclosure_id="synthetic-shelf-a",
                slot=7,
                serial="CANONICAL",
            )
            admitted_alias = ManualMapping(
                system_id=None,
                enclosure_id=DRAWER_TOP,
                slot=7,
                serial="DIVERGENT",
            )
            store._write({
                store._slot_key("synthetic-system-a", "synthetic-shelf-a", 7): canonical,
                f"{DRAWER_TOP}:7": admitted_alias,
            })
            before = store.file_path.read_bytes()

            with self.assertRaises(MappingScopeConflict):
                store.list_mappings("synthetic-system-a", DRAWER_BOTTOM)

            self.assertEqual(store.file_path.read_bytes(), before)


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

            resolved_exact = store.get_mapping(
                "system-a", "enc-a", 3, loaded_entries=loaded_entries
            )
            self.assertEqual(resolved_exact, exact)
            self.assertIsNot(resolved_exact, exact)
            self.assertIsNone(store.get_mapping("system-a", "enc-b", 3, loaded_entries=loaded_entries))
            resolved_fallback = store.get_mapping(
                "system-a",
                "enc-b",
                3,
                allow_legacy_fallback=True,
                loaded_entries=loaded_entries,
            )
            self.assertEqual(resolved_fallback, fallback)
            self.assertIsNot(resolved_fallback, fallback)
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

    def test_single_mapping_save_and_clear_require_current_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            empty_scope_revision = store.preview_replace_mappings("system-a", "enc-a", [])["revision"]
            self.assertEqual(store.scope_revision("system-a", "enc-a"), empty_scope_revision)
            save_revision = store.save_revision("system-a", "enc-a", 0)
            stale_save_revision = store.save_revision("system-a", "enc-a", 1)
            stale_clear_revision = store.clear_revision("system-a", "enc-a", 0)
            saved = store.save_mapping(
                ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=0, serial="FIRST"),
                expected_revision=save_revision,
            )
            current_revision = store.preview_replace_mappings("system-a", "enc-a", [saved])["revision"]
            self.assertEqual(store.scope_revision("system-a", "enc-a"), current_revision)
            clear_revision = store.clear_revision("system-a", "enc-a", 0)

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.save_mapping(
                    ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1, serial="STALE"),
                    expected_revision=stale_save_revision,
                )
            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.clear_mapping(
                    "system-a",
                    "enc-a",
                    0,
                    expected_revision=stale_clear_revision,
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

    def test_has_legacy_only_mapping_reports_rows_without_resolving_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store._write({
                "default:3": ManualMapping(slot=3, device_name="sda"),
                store._slot_key("system-a", "enc-a", 4): ManualMapping(
                    system_id="system-a",
                    enclosure_id="enc-a",
                    slot=4,
                    device_name="sdb",
                ),
            })

            self.assertTrue(store.has_legacy_only_mapping("system-a", "enc-a", 3))
            self.assertFalse(store.has_legacy_only_mapping("system-a", "enc-a", 4))
            self.assertFalse(store.has_legacy_only_mapping("system-a", "enc-a", 5))

            loaded_entries = store.load_all()
            store.load_all = MagicMock(side_effect=AssertionError("preloaded lookup must not reload"))  # type: ignore[method-assign]
            self.assertTrue(
                store.has_legacy_only_mapping(
                    "system-a",
                    "enc-a",
                    3,
                    loaded_entries=loaded_entries,
                )
            )
            store.load_all.assert_not_called()

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
                revision = store.save_revision("system-a", enclosure_id, 0)

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
            stale_revision = store.save_revision("default", "enc-a", 0)
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

    def test_save_revision_rejects_newer_scoped_enclosureless_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            exact_key = store._slot_key("system-a", "enc-a", 5)
            fallback_key = store._slot_key("system-a", None, 5)
            exact = ManualMapping(
                system_id="system-a",
                enclosure_id="enc-a",
                slot=5,
                serial="EXACT-OLD",
            )
            fallback = ManualMapping(
                system_id="system-a",
                enclosure_id=None,
                slot=5,
                serial="FALLBACK-OLD",
            )
            store._write({exact_key: exact, fallback_key: fallback})
            revision = store.save_revision("system-a", "enc-a", 5)
            newer_fallback = fallback.model_copy(update={"serial": "FALLBACK-NEW"})
            store._write({exact_key: exact, fallback_key: newer_fallback})

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.save_mapping(
                    exact.model_copy(update={"serial": "EXACT-NEW"}),
                    expected_revision=revision,
                )

            current = store.load_all()
            self.assertEqual(current[exact_key].serial, "EXACT-OLD")
            self.assertEqual(current[fallback_key].serial, "FALLBACK-NEW")

    def test_clear_revision_rejects_newer_shadowed_fallback_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            exact_key = store._slot_key("system-a", "enc-a", 5)
            fallback_key = store._slot_key("system-a", None, 5)
            exact = ManualMapping(
                system_id="system-a",
                enclosure_id="enc-a",
                slot=5,
                serial="EXACT",
            )
            fallback = ManualMapping(
                system_id="system-a",
                enclosure_id=None,
                slot=5,
                serial="FALLBACK-OLD",
            )
            store._write({exact_key: exact, fallback_key: fallback})
            revision = store.clear_revision("system-a", "enc-a", 5)
            newer_fallback = fallback.model_copy(update={"serial": "FALLBACK-NEW"})
            store._write({exact_key: exact, fallback_key: newer_fallback})

            with self.assertRaisesRegex(RuntimeError, "revision"):
                store.clear_mapping(
                    "system-a",
                    "enc-a",
                    5,
                    expected_revision=revision,
                )

            current = store.load_all()
            self.assertEqual(current[exact_key].serial, "EXACT")
            self.assertEqual(current[fallback_key].serial, "FALLBACK-NEW")

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
