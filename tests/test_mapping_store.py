from __future__ import annotations

import json
import os
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
import app.services.mapping_store as mapping_store_module


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
                [store._encode_v2_key("synthetic-system-a", "synthetic-shelf-a", 7)],
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
                [store._encode_v2_key("synthetic-system-a", "synthetic-shelf-a", 7)],
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
                        [store._encode_v2_key(
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
                [store._encode_v2_key("synthetic-system-a", "synthetic-shelf-a", 7)],
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


class AliasKeyModelConsistencyTests(unittest.TestCase):
    SYSTEM_A = "synthetic-system-a"
    SYSTEM_B = "synthetic-system-b"
    SHELF_A = "synthetic-shelf-a"
    SHELF_B = "synthetic-shelf-b"
    SLOT = 7

    def make_store(self, root: str) -> MappingStore:
        return MappingStore(str(Path(root) / "mappings.json"))

    def malformed_store(
        self,
        root: str,
        *,
        system_id: str | None = SYSTEM_A,
        enclosure_id: str | None = SHELF_B,
        slot: int = SLOT,
    ) -> MappingStore:
        store = self.make_store(root)
        store._write({
            f"{self.SYSTEM_A}:{DRAWER_TOP}:{self.SLOT}": ManualMapping(
                system_id=system_id,
                enclosure_id=enclosure_id,
                slot=slot,
                serial="MISMATCH",
            )
        })
        return store

    def test_each_reported_enclosure_mismatch_fails_closed_for_get_and_filter(self) -> None:
        mismatches = (
            self.SHELF_B,
            "virtual-system:pool-a",
            f"{self.SHELF_A}::unknown",
            None,
        )
        for enclosure_id in mismatches:
            with self.subTest(enclosure_id=enclosure_id), tempfile.TemporaryDirectory() as temp_dir:
                store = self.malformed_store(temp_dir, enclosure_id=enclosure_id)
                before = store.file_path.read_bytes()

                with self.assertRaises(MappingScopeConflict):
                    store.get_mapping(self.SYSTEM_A, self.SHELF_A, self.SLOT)
                with self.assertRaises(MappingScopeConflict):
                    store.list_mappings(self.SYSTEM_A, self.SHELF_A)
                with self.assertRaises(MappingScopeConflict):
                    store.list_mappings()

                self.assertEqual(store.file_path.read_bytes(), before)

    def test_foreign_model_system_and_disagreeing_model_slot_fail_closed(self) -> None:
        mismatches = (
            {"system_id": self.SYSTEM_B, "enclosure_id": self.SHELF_A, "slot": self.SLOT},
            {"system_id": self.SYSTEM_A, "enclosure_id": self.SHELF_A, "slot": self.SLOT + 1},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temp_dir:
                store = self.malformed_store(temp_dir, **mismatch)
                before = store.file_path.read_bytes()

                with self.assertRaises(MappingScopeConflict):
                    store.get_mapping(self.SYSTEM_A, self.SHELF_A, self.SLOT)

                self.assertEqual(store.file_path.read_bytes(), before)

    def test_save_rejects_foreign_model_system_before_deleting_alias_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.malformed_store(
                temp_dir,
                system_id=self.SYSTEM_B,
                enclosure_id=self.SHELF_A,
            )
            before = store.file_path.read_bytes()

            with self.assertRaises(MappingScopeConflict):
                store.save_mapping(ManualMapping(
                    system_id=self.SYSTEM_A,
                    enclosure_id=self.SHELF_A,
                    slot=self.SLOT,
                    serial="NEW",
                ))

            self.assertEqual(store.file_path.read_bytes(), before)

    def test_consistent_canonical_and_drawer_alias_rows_remain_accepted(self) -> None:
        rows = (
            (self.SHELF_A, self.SHELF_A),
            (DRAWER_TOP, DRAWER_TOP),
            (DRAWER_BOTTOM, self.SHELF_A),
        )
        for keyed_enclosure_id, model_enclosure_id in rows:
            with (
                self.subTest(
                    keyed_enclosure_id=keyed_enclosure_id,
                    model_enclosure_id=model_enclosure_id,
                ),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                store._write({
                    f"{self.SYSTEM_A}:{keyed_enclosure_id}:{self.SLOT}": ManualMapping(
                        system_id=self.SYSTEM_A,
                        enclosure_id=model_enclosure_id,
                        slot=self.SLOT,
                        serial="VALID",
                    )
                })

                mapping = store.get_mapping(self.SYSTEM_A, self.SHELF_A, self.SLOT)

                self.assertIsNotNone(mapping)
                assert mapping is not None
                self.assertEqual(mapping.enclosure_id, self.SHELF_A)

    def test_system_scoped_key_with_legacy_none_model_system_remains_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store._write({
                f"{self.SYSTEM_A}:{DRAWER_TOP}:{self.SLOT}": ManualMapping(
                    system_id=None,
                    enclosure_id=DRAWER_BOTTOM,
                    slot=self.SLOT,
                    serial="LEGACY-SCOPED",
                )
            })

            mapping = store.get_mapping(self.SYSTEM_A, self.SHELF_A, self.SLOT)

            self.assertIsNotNone(mapping)
            assert mapping is not None
            self.assertEqual(mapping.serial, "LEGACY-SCOPED")
            self.assertEqual(
                [item.serial for item in store.list_mappings()],
                ["LEGACY-SCOPED"],
            )

    def test_selected_scope_operations_reject_inconsistent_row_before_mutation(self) -> None:
        operations = (
            lambda store: store.save_mapping(ManualMapping(
                system_id=self.SYSTEM_A,
                enclosure_id=self.SHELF_A,
                slot=self.SLOT,
                serial="NEW",
            )),
            lambda store: store.clear_mapping(self.SYSTEM_A, self.SHELF_A, self.SLOT),
            lambda store: store.replace_mappings(self.SYSTEM_A, self.SHELF_A, []),
            lambda store: store.preview_replace_mappings(self.SYSTEM_A, self.SHELF_A, []),
            lambda store: store.apply_mapping_import(
                self.SYSTEM_A,
                self.SHELF_A,
                [],
                expected_revision="0" * 64,
                import_digest="1" * 64,
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                store = self.malformed_store(temp_dir)
                before = store.file_path.read_bytes()

                with self.assertRaises(MappingScopeConflict):
                    operation(store)

                self.assertEqual(store.file_path.read_bytes(), before)

    def test_revision_and_digest_issuance_reject_inconsistent_removable_row(self) -> None:
        operations = (
            lambda store: store.scope_revision(self.SYSTEM_A, self.SHELF_A),
            lambda store: store.save_revision(self.SYSTEM_A, self.SHELF_A, self.SLOT),
            lambda store: store.clear_revision(self.SYSTEM_A, self.SHELF_A, self.SLOT),
            lambda store: store.save_revisions(
                self.SYSTEM_A, [(self.SHELF_A, self.SLOT)]
            ),
            lambda store: store.clear_revisions(
                self.SYSTEM_A, [(self.SHELF_A, self.SLOT)]
            ),
            lambda store: store.preview_replace_mappings(self.SYSTEM_A, self.SHELF_A, []),
        )
        for operation in operations:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                store = self.malformed_store(temp_dir)
                before = store.file_path.read_bytes()

                with self.assertRaises(MappingScopeConflict):
                    operation(store)

                self.assertEqual(store.file_path.read_bytes(), before)

    def test_different_physical_scope_does_not_admit_malformed_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.malformed_store(temp_dir)
            before = store.file_path.read_bytes()

            self.assertEqual(store.list_mappings(self.SYSTEM_A, "synthetic-shelf-c"), [])
            self.assertIsNone(
                store.get_mapping(self.SYSTEM_A, "synthetic-shelf-c", self.SLOT)
            )
            self.assertEqual(store.file_path.read_bytes(), before)

    def test_different_system_scope_does_not_admit_foreign_model_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.malformed_store(
                temp_dir,
                system_id=self.SYSTEM_B,
                enclosure_id=self.SHELF_A,
            )
            before = store.file_path.read_bytes()

            self.assertEqual(store.list_mappings("synthetic-system-c"), [])
            self.assertEqual(store.file_path.read_bytes(), before)

    def test_malformed_row_outside_selected_physical_or_system_scope_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.malformed_store(temp_dir)
            unrelated = ManualMapping(
                system_id=self.SYSTEM_B,
                enclosure_id=self.SHELF_B,
                slot=self.SLOT,
                serial="UNRELATED",
            )
            current = store.load_all()
            current[store._slot_key(self.SYSTEM_B, self.SHELF_B, self.SLOT)] = unrelated
            current[store._slot_key(
                self.SYSTEM_A, "synthetic-shelf-c", self.SLOT
            )] = unrelated.model_copy(
                update={
                    "system_id": self.SYSTEM_A,
                    "enclosure_id": "synthetic-shelf-c",
                    "serial": "OTHER-PHYSICAL",
                }
            )
            store._write(current)
            before = store.file_path.read_bytes()

            self.assertEqual(
                [mapping.serial for mapping in store.list_mappings(self.SYSTEM_B, self.SHELF_B)],
                ["UNRELATED"],
            )
            self.assertEqual(
                [mapping.serial for mapping in store.list_mappings(self.SYSTEM_B)],
                ["UNRELATED"],
            )
            mapping = store.get_mapping(self.SYSTEM_B, self.SHELF_B, self.SLOT)
            self.assertIsNotNone(mapping)
            assert mapping is not None
            self.assertEqual(mapping.serial, "UNRELATED")
            self.assertEqual(
                [mapping.serial for mapping in store.list_mappings(
                    self.SYSTEM_A, "synthetic-shelf-c"
                )],
                ["OTHER-PHYSICAL"],
            )
            self.assertEqual(store.file_path.read_bytes(), before)

    def test_reverse_key_model_mismatches_fail_closed_on_every_scope_surface(self) -> None:
        mismatches = (
            (f"{self.SYSTEM_A}:unknown-shelf:{self.SLOT}", self.SYSTEM_A, self.SHELF_A, self.SLOT),
            (f"{self.SYSTEM_B}:{self.SHELF_A}:{self.SLOT}", self.SYSTEM_A, self.SHELF_A, self.SLOT),
            (f"{self.SYSTEM_A}:{self.SHELF_A}:{self.SLOT + 1}", self.SYSTEM_A, self.SHELF_A, self.SLOT),
        )
        operations = (
            lambda store: store.get_mapping(self.SYSTEM_A, self.SHELF_A, self.SLOT),
            lambda store: store.list_mappings(self.SYSTEM_A, self.SHELF_A),
            lambda store: store.scope_revision(self.SYSTEM_A, self.SHELF_A),
            lambda store: store.save_revision(self.SYSTEM_A, self.SHELF_A, self.SLOT),
            lambda store: store.clear_revision(self.SYSTEM_A, self.SHELF_A, self.SLOT),
            lambda store: store.save_revisions(self.SYSTEM_A, [(self.SHELF_A, self.SLOT)]),
            lambda store: store.clear_revisions(self.SYSTEM_A, [(self.SHELF_A, self.SLOT)]),
            lambda store: store.preview_replace_mappings(self.SYSTEM_A, self.SHELF_A, []),
            lambda store: store.save_mapping(ManualMapping(
                system_id=self.SYSTEM_A,
                enclosure_id=self.SHELF_A,
                slot=self.SLOT,
                serial="NEW",
            )),
            lambda store: store.clear_mapping(self.SYSTEM_A, self.SHELF_A, self.SLOT),
            lambda store: store.replace_mappings(self.SYSTEM_A, self.SHELF_A, []),
            lambda store: store.apply_mapping_import(
                self.SYSTEM_A,
                self.SHELF_A,
                [],
                expected_revision="0" * 64,
                import_digest="1" * 64,
            ),
        )
        for key, system_id, enclosure_id, slot in mismatches:
            for operation in operations:
                with (
                    self.subTest(key=key, operation=operation),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    store = self.make_store(temp_dir)
                    valid = ManualMapping(
                        system_id=self.SYSTEM_A,
                        enclosure_id=self.SHELF_A,
                        slot=self.SLOT,
                        serial="VALID",
                    )
                    hidden = ManualMapping(
                        system_id=system_id,
                        enclosure_id=enclosure_id,
                        slot=slot,
                        serial="HIDDEN",
                    )
                    store._write({
                        store._slot_key(
                            self.SYSTEM_A, self.SHELF_A, self.SLOT
                        ): valid,
                        key: hidden,
                    })
                    before = store.file_path.read_bytes()

                    with self.assertRaises(MappingScopeConflict):
                        operation(store)

                    self.assertEqual(store.file_path.read_bytes(), before)

    def test_preissued_tokens_cannot_confirm_hidden_model_scoped_deletions(self) -> None:
        operations = ("import", "replace", "clear")
        for operation in operations:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                valid = ManualMapping(
                    system_id=self.SYSTEM_A,
                    enclosure_id=self.SHELF_A,
                    slot=self.SLOT,
                    serial="VALID",
                )
                store._write({
                    store._slot_key(self.SYSTEM_A, self.SHELF_A, self.SLOT): valid,
                })
                preview = store.preview_replace_mappings(self.SYSTEM_A, self.SHELF_A, [])
                clear_revision = store.clear_revision(
                    self.SYSTEM_A, self.SHELF_A, self.SLOT
                )
                current = store.load_all()
                current[f"{self.SYSTEM_B}:unknown-shelf:{self.SLOT}"] = valid.model_copy(
                    update={"serial": "HIDDEN"}
                )
                store._write(current)
                before = store.file_path.read_bytes()

                with self.assertRaises(MappingScopeConflict):
                    if operation == "import":
                        store.apply_mapping_import(
                            self.SYSTEM_A,
                            self.SHELF_A,
                            [],
                            expected_revision=preview["revision"],
                            import_digest=preview["import_digest"],
                        )
                    elif operation == "replace":
                        store.replace_mappings(self.SYSTEM_A, self.SHELF_A, [])
                    else:
                        store.clear_mapping(
                            self.SYSTEM_A,
                            self.SHELF_A,
                            self.SLOT,
                            expected_revision=clear_revision,
                        )

                self.assertEqual(store.file_path.read_bytes(), before)

    def test_colon_bearing_neighbor_scopes_do_not_prefix_collide(self) -> None:
        cases = (
            ("system:a", "enc:extra", "enc:other", 1),
            ("system:a", "enc:extra::node", "enc:other::node", 10),
        )
        operations = (
            lambda store: store.get_mapping("system:a", "enc", 1),
            lambda store: store.list_mappings("system:a", "enc"),
            lambda store: store.scope_revision("system:a", "enc"),
            lambda store: store.save_revision("system:a", "enc", 1),
            lambda store: store.clear_revision("system:a", "enc", 1),
            lambda store: store.save_revisions("system:a", [("enc", 1), ("enc", 10)]),
            lambda store: store.clear_revisions("system:a", [("enc", 1), ("enc", 10)]),
            lambda store: store.preview_replace_mappings("system:a", "enc", []),
        )
        for operation in operations:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                rows = {
                    store._slot_key(system_id, keyed_enclosure, slot): ManualMapping(
                        system_id=system_id,
                        enclosure_id=model_enclosure,
                        slot=slot,
                        serial=f"OUTSIDE-{slot}",
                    )
                    for system_id, keyed_enclosure, model_enclosure, slot in cases
                }
                rows[store._slot_key("system:a", "enc:other", 10)] = ManualMapping(
                    system_id="system:a",
                    enclosure_id="enc:other",
                    slot=10,
                    serial="VALID-OUTSIDE",
                )
                rows[store._slot_key("system:b", "enc", 1)] = ManualMapping(
                    system_id="system:b",
                    enclosure_id="enc",
                    slot=1,
                    serial="OTHER-SYSTEM",
                )
                store._write(rows)
                before = store.file_path.read_bytes()

                result = operation(store)

                self.assertNotIn("OUTSIDE", repr(result))
                self.assertNotIn("OTHER-SYSTEM", repr(result))
                self.assertEqual(store.file_path.read_bytes(), before)

    def test_valid_colon_and_double_colon_scopes_remain_exact_and_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            rows = {
                store._slot_key("system:a", "enc:extra", 1): ManualMapping(
                    system_id="system:a",
                    enclosure_id="enc:extra",
                    slot=1,
                    serial="ONE",
                ),
                store._slot_key("system:a", "enc:extra::node", 10): ManualMapping(
                    system_id="system:a",
                    enclosure_id="enc:extra::node",
                    slot=10,
                    serial="TEN",
                ),
            }
            store._write(rows)

            self.assertEqual(
                [mapping.serial for mapping in store.list_mappings()],
                ["ONE", "TEN"],
            )
            self.assertEqual(
                [mapping.serial for mapping in store.list_mappings(
                    "system:a", "enc:extra"
                )],
                ["ONE"],
            )


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

    def test_exact_key_with_foreign_model_system_fails_closed(self) -> None:
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
            before = store.file_path.read_bytes()

            with self.assertRaises(MappingScopeConflict):
                store.get_mapping("system-b", "enc-b", 3)

            self.assertEqual(store.file_path.read_bytes(), before)

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
                    [store._encode_v2_key("system-a", enclosure_id, 0)],
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
                [store._encode_v2_key("system-a", "enc-a", 0)],
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
            self.assertNotIn(store._encode_v2_key("system-a", None, 5), current)
            self.assertEqual(list(current), [store._encode_v2_key("system-a", "enc-a", 5)])

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


class MappingStoreInjectiveKeyV2Tests(unittest.TestCase):
    def make_store(self, root: str) -> MappingStore:
        return MappingStore(str(Path(root) / "mappings.json"))

    @staticmethod
    def write_document(store: MappingStore, version: int, rows: dict[str, ManualMapping]) -> bytes:
        payload = {
            "version": version,
            "updated_at": "2026-09-05T00:00:00+00:00",
            "slot_mappings": {
                key: mapping.model_dump(mode="json") for key, mapping in rows.items()
            },
        }
        raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        store.file_path.write_bytes(raw)
        return raw

    def test_v2_keys_are_injective_and_round_trip_exactly(self) -> None:
        cases = (
            (None, None, 1),
            ("", "", 10),
            ("default_system", "default", 1),
            ("system:a", "enc:part", 1),
            ("system::a", "enc::part", 10),
            ('sys"quote', "enc\\slash", 1),
            ("systém", "棚", 10),
            ("x" * 2048, "y" * 2048, 1),
            ("v2", "v2:", 10),
        )
        encoded = [MappingStore._encode_v2_key(*identity) for identity in cases]
        self.assertEqual(len(encoded), len(set(encoded)))
        for identity, key in zip(cases, encoded, strict=True):
            with self.subTest(identity=identity):
                self.assertEqual(key, "v2:" + json.dumps(
                    list(identity), ensure_ascii=True, separators=(",", ":")
                ))
                self.assertEqual(MappingStore._decode_v2_key(key), identity)

    def test_v2_decoder_rejects_noncanonical_or_malformed_keys(self) -> None:
        malformed = (
            "v1:[\"s\",\"e\",1]",
            "v2: [\"s\",\"e\",1]",
            "v2:[\"s\", \"e\",1]",
            "v2:[\"s\",\"e\",1] ",
            "v2:[\"s\",\"e\",1]null",
            "v2:[\"s\",\"e\"]",
            "v2:[\"s\",\"e\",1,2]",
            "v2:{\"0\":\"s\"}",
            "v2:[1,\"e\",1]",
            "v2:[\"s\",1,1]",
            "v2:[\"s\",\"e\",true]",
            "v2:[\"s\",\"e\",1.0]",
            "v2:[\"s\",\"\\u0065\",1]",
            "v2:[\"s\",\"é\",1]",
            "v2:not-json",
        )
        for key in malformed:
            with self.subTest(key=key), self.assertRaises(MappingScopeConflict):
                MappingStore._decode_v2_key(key)

    def test_v2_key_model_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            model = ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1)
            key = "v2:" + json.dumps(["system-b", "enc-a", 1], separators=(",", ":"))
            before = self.write_document(store, 2, {key: model})
            with self.assertRaises(MappingScopeConflict):
                store.list_mappings()
            self.assertEqual(store.file_path.read_bytes(), before)

    def test_v1_reads_do_not_mutate_and_first_write_migrates_complete_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            rows = {
                "system:a:enc:part:1": ManualMapping(
                    system_id="system:a", enclosure_id="enc:part", slot=1, serial="SELECTED"
                ),
                "system-b:enc-b:10": ManualMapping(
                    system_id="system-b", enclosure_id="enc-b", slot=10, serial="FOREIGN"
                ),
                "legacy:enc:1": ManualMapping(
                    system_id=None, enclosure_id="legacy:enc", slot=1, serial="LEGACY"
                ),
            }
            before = self.write_document(store, 1, rows)

            self.assertEqual(store.get_mapping("system:a", "enc:part", 1).serial, "SELECTED")
            self.assertEqual(len(store.list_mappings()), 3)
            self.assertEqual(store.file_path.read_bytes(), before)

            store.save_mapping(ManualMapping(
                system_id="system-c", enclosure_id="enc-c", slot=1, serial="NEW"
            ))
            payload = json.loads(store.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(len(payload["slot_mappings"]), 4)
            self.assertTrue(all(key.startswith("v2:") for key in payload["slot_mappings"]))
            self.assertEqual(
                {mapping.serial for mapping in store.load_all().values()},
                {"SELECTED", "FOREIGN", "LEGACY", "NEW"},
            )

    def test_v1_collision_row_is_classified_by_its_model_on_every_surface(self) -> None:
        collision_key = "system:a:enc:part:1"
        variants = (
            (ManualMapping(system_id="system:a", enclosure_id="enc:part", slot=1, serial="SELECTED"), True),
            (ManualMapping(system_id="system:a:enc", enclosure_id="part", slot=1, serial="FOREIGN"), False),
            (ManualMapping(system_id=None, enclosure_id="system:a:enc:part", slot=1, serial="LEGACY"), False),
        )
        for row, selected in variants:
            with self.subTest(row=row.serial), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                self.write_document(store, 1, {collision_key: row})
                before = store.file_path.read_bytes()

                resolved = store.get_mapping("system:a", "enc:part", 1, allow_legacy_fallback=True)
                listed = store.list_mappings("system:a", "enc:part")
                store.save_revision("system:a", "enc:part", 1)
                store.clear_revision("system:a", "enc:part", 1)
                preview = store.preview_replace_mappings("system:a", "enc:part", [])
                self.assertEqual(resolved is not None, selected)
                self.assertEqual(bool(listed), selected)
                self.assertEqual(bool(preview["removals"]), selected)
                self.assertEqual(store.file_path.read_bytes(), before)

                store.replace_mappings("system:a", "enc:part", [])
                remaining = store.list_mappings()
                self.assertEqual(bool(remaining), not selected)
                if remaining:
                    self.assertEqual(remaining[0].serial, row.serial)

    def test_v1_equal_aliases_collapse_and_divergent_aliases_fail_before_write(self) -> None:
        for divergent in (False, True):
            with self.subTest(divergent=divergent), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                canonical = ManualMapping(
                    system_id="system-a", enclosure_id="synthetic-shelf-a", slot=7, serial="SAME"
                )
                alias = canonical.model_copy(update={
                    "enclosure_id": DRAWER_TOP,
                    "serial": "OTHER" if divergent else "SAME",
                })
                before = self.write_document(store, 1, {
                    "system-a:synthetic-shelf-a:7": canonical,
                    f"system-a:{DRAWER_TOP}:7": alias,
                })
                if divergent:
                    with self.assertRaises(MappingScopeConflict):
                        store.save_mapping(ManualMapping(
                            system_id="system-b", enclosure_id="enc-b", slot=1
                        ))
                    self.assertEqual(store.file_path.read_bytes(), before)
                else:
                    store.save_mapping(ManualMapping(
                        system_id="system-b", enclosure_id="enc-b", slot=1
                    ))
                    payload = json.loads(store.file_path.read_text(encoding="utf-8"))
                    self.assertEqual(len(payload["slot_mappings"]), 2)

    def test_orphan_temps_are_ignored_and_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            orphan = store.file_path.parent / f"{store.file_path.name}.untrusted.tmp"
            orphan.write_text("untrusted", encoding="utf-8")
            store.save_mapping(ManualMapping(system_id="system-a", enclosure_id="enc-a", slot=1))
            self.assertEqual(orphan.read_text(encoding="utf-8"), "untrusted")

    def test_atomic_writer_failure_boundaries_preserve_defined_restart_state(self) -> None:
        self.assertTrue(hasattr(mapping_store_module, "MappingDurabilityError"))
        durability_error = mapping_store_module.MappingDurabilityError
        pre_replace = ("_serialize_v2", "_create_temp_file", "_write_temp_file")
        for method in pre_replace:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                old = self.write_document(store, 1, {
                    "system-a:enc-a:1": ManualMapping(
                        system_id="system-a", enclosure_id="enc-a", slot=1, serial="OLD"
                    )
                })
                with patch.object(store, method, side_effect=OSError("synthetic failure")):
                    with self.assertRaises(OSError):
                        store.save_mapping(ManualMapping(
                            system_id="system-b", enclosure_id="enc-b", slot=1
                        ))
                self.assertEqual(store.file_path.read_bytes(), old)
                self.assertEqual(
                    list(store.file_path.parent.glob(f"{store.file_path.name}.*.tmp")), []
                )
                self.assertEqual(store.get_mapping("system-a", "enc-a", 1).serial, "OLD")

        for method in ("_fsync_parent_directory", "_validate_published_bytes"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                self.write_document(store, 1, {
                    "system-a:enc-a:1": ManualMapping(
                        system_id="system-a", enclosure_id="enc-a", slot=1, serial="OLD"
                    )
                })
                with patch.object(store, method, side_effect=OSError("synthetic failure")):
                    with self.assertRaises(durability_error):
                        store.save_mapping(ManualMapping(
                            system_id="system-b", enclosure_id="enc-b", slot=1, serial="NEW"
                        ))
                payload = json.loads(store.file_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["version"], 2)
                restarted = self.make_store(temp_dir)
                self.assertEqual(
                    {mapping.serial for mapping in restarted.load_all().values()},
                    {"OLD", "NEW"},
                )

    def test_collision_save_clear_replace_and_import_never_remove_other_identities(self) -> None:
        collision_key = "system:a:enc:part:1"
        variants = (
            ManualMapping(system_id="system:a", enclosure_id="enc:part", slot=1, serial="SELECTED"),
            ManualMapping(system_id="system:a:enc", enclosure_id="part", slot=1, serial="FOREIGN"),
            ManualMapping(system_id=None, enclosure_id="system:a:enc:part", slot=1, serial="LEGACY"),
        )
        for original in variants:
            for operation in ("save", "clear", "replace", "import"):
                with (
                    self.subTest(original=original.serial, operation=operation),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    store = self.make_store(temp_dir)
                    self.write_document(store, 1, {collision_key: original})
                    selected = ManualMapping(
                        system_id="system:a",
                        enclosure_id="enc:part",
                        slot=1,
                        serial="NEW",
                    )
                    if operation == "save":
                        store.save_mapping(selected)
                    elif operation == "clear":
                        store.clear_mapping("system:a", "enc:part", 1)
                    elif operation == "replace":
                        store.replace_mappings("system:a", "enc:part", [])
                    else:
                        preview = store.preview_replace_mappings(
                            "system:a", "enc:part", []
                        )
                        store.apply_mapping_import(
                            "system:a",
                            "enc:part",
                            [],
                            expected_revision=preview["revision"],
                            import_digest=preview["import_digest"],
                        )
                    serials = {
                        mapping.serial for mapping in store.list_mappings()
                    }
                    if original.serial == "SELECTED":
                        self.assertEqual(
                            serials,
                            {"NEW"} if operation == "save" else set(),
                        )
                    elif operation == "save":
                        self.assertEqual(serials, {original.serial, "NEW"})
                    else:
                        self.assertEqual(serials, {original.serial})

    def test_atomic_writer_uses_exclusive_owner_only_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            observed: dict[str, int] = {}

            def inspect_before_replace(temp_path: Path) -> None:
                observed["mode"] = os.stat(temp_path).st_mode & 0o777
                raise OSError("synthetic replace failure")

            with patch.object(store, "_replace_temp_file", side_effect=inspect_before_replace):
                with self.assertRaises(OSError):
                    store.save_mapping(ManualMapping(
                        system_id="system-a", enclosure_id="enc-a", slot=1
                    ))
            self.assertEqual(observed, {"mode": 0o600})
            self.assertFalse(store.file_path.exists())
            self.assertEqual(
                list(store.file_path.parent.glob(f"{store.file_path.name}.*.tmp")), []
            )

    def test_all_pre_replace_fault_boundaries_keep_exact_v1_target(self) -> None:
        methods = (
            "_classify_row",
            "_serialize_v2",
            "_create_temp_file",
            "_write_temp_bytes",
            "_flush_temp_file",
            "_fsync_temp_file",
            "_replace_temp_file",
        )
        for method in methods:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                old = self.write_document(store, 1, {
                    "system-a:enc-a:1": ManualMapping(
                        system_id="system-a", enclosure_id="enc-a", slot=1, serial="OLD"
                    )
                })
                with patch.object(store, method, side_effect=OSError("synthetic failure")):
                    with self.assertRaises(OSError):
                        store.save_mapping(ManualMapping(
                            system_id="system-b", enclosure_id="enc-b", slot=1
                        ))
                self.assertEqual(store.file_path.read_bytes(), old)
                self.assertEqual(
                    list(store.file_path.parent.glob(f"{store.file_path.name}.*.tmp")), []
                )
                restarted = self.make_store(temp_dir)
                resolved = restarted.get_mapping("system-a", "enc-a", 1)
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved.serial, "OLD")

    def test_classification_failure_precedes_temp_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            before = self.write_document(store, 1, {
                "wrong:key": ManualMapping(
                    system_id="system-a", enclosure_id="enc-a", slot=1
                )
            })
            with patch.object(
                store,
                "_create_temp_file",
                side_effect=AssertionError("classification must precede temp creation"),
            ) as create:
                with self.assertRaises(MappingScopeConflict):
                    store.save_mapping(ManualMapping(
                        system_id="system-b", enclosure_id="enc-b", slot=1
                    ))
            create.assert_not_called()
            self.assertEqual(store.file_path.read_bytes(), before)

    def test_v1_tokens_are_stale_after_an_unrelated_confirmed_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            selected = ManualMapping(
                system_id="system-a", enclosure_id="enc-a", slot=1, serial="OLD"
            )
            self.write_document(store, 1, {"system-a:enc-a:1": selected})
            save_revision = store.save_revision("system-a", "enc-a", 1)
            clear_revision = store.clear_revision("system-a", "enc-a", 1)
            preview = store.preview_replace_mappings("system-a", "enc-a", [selected])

            store.save_mapping(ManualMapping(
                system_id="system-b", enclosure_id="enc-b", slot=1, serial="MIGRATE"
            ))

            with self.assertRaises(MappingRevisionConflict):
                store.save_mapping(selected, expected_revision=save_revision)
            with self.assertRaises(MappingRevisionConflict):
                store.clear_mapping(
                    "system-a", "enc-a", 1, expected_revision=clear_revision
                )
            with self.assertRaises(MappingRevisionConflict):
                store.apply_mapping_import(
                    "system-a",
                    "enc-a",
                    [selected],
                    expected_revision=preview["revision"],
                    import_digest=preview["import_digest"],
                )

    def test_collision_identity_change_invalidates_preview_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            key = "system:a:enc:part:1"
            foreign = ManualMapping(
                system_id="system:a:enc", enclosure_id="part", slot=1, serial="FOREIGN"
            )
            selected = ManualMapping(
                system_id="system:a", enclosure_id="enc:part", slot=1, serial="SELECTED"
            )
            self.write_document(store, 1, {key: foreign})
            preview = store.preview_replace_mappings("system:a", "enc:part", [])
            self.write_document(store, 1, {key: selected})

            with self.assertRaises(MappingRevisionConflict):
                store.apply_mapping_import(
                    "system:a",
                    "enc:part",
                    [],
                    expected_revision=preview["revision"],
                    import_digest=preview["import_digest"],
                )

    def test_v2_rewrites_remain_canonical_and_identity_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            rows = [
                ManualMapping(system_id="system:a", enclosure_id="enc:part", slot=1, serial="ONE"),
                ManualMapping(system_id="system:b", enclosure_id=None, slot=10, serial="TEN"),
            ]
            store.replace_mappings("system:a", None, rows[:1])
            store.replace_mappings("system:b", None, rows[1:])
            first_keys = set(json.loads(
                store.file_path.read_text(encoding="utf-8")
            )["slot_mappings"])
            store.replace_mappings("system:a", None, rows[:1])
            second_payload = json.loads(store.file_path.read_text(encoding="utf-8"))
            self.assertEqual(second_payload["version"], 2)
            self.assertEqual(set(second_payload["slot_mappings"]), first_keys)
            for key in first_keys:
                self.assertEqual(
                    MappingStore._encode_v2_key(*MappingStore._decode_v2_key(key)),
                    key,
                )


class MappingStoreAuthoritativeLoadTests(unittest.TestCase):
    SYSTEM_ID = "synthetic-system-a"
    ENCLOSURE_ID = "synthetic-enclosure-a"
    SLOT = 7

    def make_store(self, root: str) -> MappingStore:
        return MappingStore(Path(root) / "mappings.json")

    def mapping(self, serial: str = "OLD") -> ManualMapping:
        return ManualMapping(
            system_id=self.SYSTEM_ID,
            enclosure_id=self.ENCLOSURE_ID,
            slot=self.SLOT,
            serial=serial,
        )

    def write_valid_document(self, store: MappingStore, version: int) -> bytes:
        mapping = self.mapping()
        key = (
            store._slot_key(self.SYSTEM_ID, self.ENCLOSURE_ID, self.SLOT)
            if version == 1
            else store._encode_v2_key(self.SYSTEM_ID, self.ENCLOSURE_ID, self.SLOT)
        )
        payload = {
            "version": version,
            "updated_at": "2026-09-05T00:00:00+00:00",
            "slot_mappings": {key: mapping.model_dump(mode="json")},
        }
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        store.file_path.write_bytes(raw)
        return raw

    @staticmethod
    def temp_paths(store: MappingStore) -> set[Path]:
        return set(store.file_path.parent.glob(f"{store.file_path.name}.*.tmp"))

    def mutation(self, store: MappingStore, operation: str) -> object:
        incoming = [self.mapping("NEW")]
        if operation == "save":
            return store.save_mapping(incoming[0])
        if operation == "clear":
            return store.clear_mapping(
                self.SYSTEM_ID,
                self.ENCLOSURE_ID,
                self.SLOT,
            )
        if operation == "replace":
            return store.replace_mappings(
                self.SYSTEM_ID,
                self.ENCLOSURE_ID,
                incoming,
            )
        if operation == "import":
            return store.apply_mapping_import(
                self.SYSTEM_ID,
                self.ENCLOSURE_ID,
                incoming,
                expected_revision="0" * 64,
                import_digest="1" * 64,
            )
        raise AssertionError(f"unknown mutation {operation}")

    def authority_surface(self, store: MappingStore, operation: str) -> object:
        if operation in {"save", "clear", "replace", "import"}:
            return self.mutation(store, operation)
        if operation == "scope_revision":
            return store.scope_revision(self.SYSTEM_ID, self.ENCLOSURE_ID)
        if operation == "save_revision":
            return store.save_revision(
                self.SYSTEM_ID,
                self.ENCLOSURE_ID,
                self.SLOT,
            )
        if operation == "clear_revision":
            return store.clear_revision(
                self.SYSTEM_ID,
                self.ENCLOSURE_ID,
                self.SLOT,
            )
        if operation == "save_revisions":
            return store.save_revisions(
                self.SYSTEM_ID,
                [(self.ENCLOSURE_ID, self.SLOT)],
            )
        if operation == "clear_revisions":
            return store.clear_revisions(
                self.SYSTEM_ID,
                [(self.ENCLOSURE_ID, self.SLOT)],
            )
        if operation == "preview":
            return store.preview_replace_mappings(
                self.SYSTEM_ID,
                self.ENCLOSURE_ID,
                [self.mapping("NEW")],
            )
        raise AssertionError(f"unknown authority surface {operation}")

    def test_initial_source_read_errors_fail_closed_for_every_authority_surface(self) -> None:
        original_read_bytes = Path.read_bytes
        original_replace = os.replace
        operations = (
            "save",
            "clear",
            "replace",
            "import",
            "scope_revision",
            "save_revision",
            "clear_revision",
            "save_revisions",
            "clear_revisions",
            "preview",
        )
        failures = (OSError("synthetic read failure"), PermissionError("synthetic denied"))
        for version in (1, 2):
            for failure in failures:
                for operation in operations:
                    with (
                        self.subTest(
                            version=version,
                            failure=type(failure).__name__,
                            operation=operation,
                        ),
                        tempfile.TemporaryDirectory() as temp_dir,
                    ):
                        store = self.make_store(temp_dir)
                        before = self.write_valid_document(store, version)
                        before_temps = self.temp_paths(store)
                        target_reads = 0

                        def fail_initial_target_read(path: Path) -> bytes:
                            nonlocal target_reads
                            if path == store.file_path:
                                target_reads += 1
                                if target_reads == 1:
                                    raise failure
                            return original_read_bytes(path)

                        with (
                            patch.object(Path, "read_bytes", fail_initial_target_read),
                            patch.object(
                                store,
                                "_create_temp_file",
                                wraps=store._create_temp_file,
                            ) as create_temp,
                            patch.object(
                                store,
                                "_replace_temp_file",
                                wraps=store._replace_temp_file,
                            ) as replace_temp,
                            patch.object(
                                mapping_store_module.os,
                                "replace",
                                wraps=original_replace,
                            ) as replace_path,
                            self.assertRaises(type(failure)),
                        ):
                            self.authority_surface(store, operation)

                        create_temp.assert_not_called()
                        replace_temp.assert_not_called()
                        replace_path.assert_not_called()
                        self.assertEqual(original_read_bytes(store.file_path), before)
                        self.assertEqual(self.temp_paths(store), before_temps)
                        restarted = self.make_store(temp_dir)
                        self.assertEqual(
                            [item.serial for item in restarted.list_mappings()],
                            ["OLD"],
                        )

    def test_malformed_documents_fail_closed_on_mutations_and_token_issuers(self) -> None:
        original_replace = os.replace
        model = self.mapping().model_dump(mode="json")
        model_json = json.dumps(model, separators=(",", ":"))
        duplicate_model_json = model_json.replace(
            f'"slot":{self.SLOT}',
            f'"slot":{self.SLOT},"slot":{self.SLOT}',
            1,
        )
        v2_key = MappingStore._encode_v2_key(
            self.SYSTEM_ID,
            self.ENCLOSURE_ID,
            self.SLOT,
        )
        malformed = {
            "invalid_utf8": b"\xff",
            "truncated_json": b'{"version":2',
            "non_object_root": b"[]",
            "missing_slot_mappings": b'{"version":2}',
            "non_object_slot_mappings": b'{"version":2,"slot_mappings":[]}',
            "unsupported_version": b'{"version":3,"slot_mappings":{}}',
            "boolean_version": b'{"version":true,"slot_mappings":{}}',
            "invalid_v2_key": (
                '{"version":2,"slot_mappings":{"not-v2":' + model_json + "}}"
            ).encode("utf-8"),
            "invalid_v2_model": (
                '{"version":2,"slot_mappings":'
                + json.dumps({v2_key: {**model, "slot": True}}, separators=(",", ":"))
                + "}"
            ).encode("utf-8"),
            "duplicate_root_key": (
                '{"version":2,"version":2,"slot_mappings":{}}'
            ).encode("utf-8"),
            "duplicate_mapping_key": (
                '{"version":2,"slot_mappings":{'
                + json.dumps(v2_key)
                + ":"
                + model_json
                + ","
                + json.dumps(v2_key)
                + ":"
                + model_json
                + "}}"
            ).encode("utf-8"),
            "duplicate_mapping_entry_key": (
                '{"version":2,"slot_mappings":{'
                + json.dumps(v2_key)
                + ":"
                + duplicate_model_json
                + "}}"
            ).encode("utf-8"),
        }
        operations = (
            "save",
            "clear",
            "replace",
            "import",
            "scope_revision",
            "save_revision",
            "clear_revision",
            "save_revisions",
            "clear_revisions",
            "preview",
        )
        for case, raw in malformed.items():
            for operation in operations:
                with (
                    self.subTest(case=case, operation=operation),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    store = self.make_store(temp_dir)
                    store.file_path.write_bytes(raw)
                    before_temps = self.temp_paths(store)
                    with (
                        patch.object(
                            store,
                            "_create_temp_file",
                            wraps=store._create_temp_file,
                        ) as create_temp,
                        patch.object(
                            store,
                            "_replace_temp_file",
                            wraps=store._replace_temp_file,
                        ) as replace_temp,
                        patch.object(
                            mapping_store_module.os,
                            "replace",
                            wraps=original_replace,
                        ) as replace_path,
                        self.assertRaises(MappingScopeConflict),
                    ):
                        self.authority_surface(store, operation)

                    create_temp.assert_not_called()
                    replace_temp.assert_not_called()
                    replace_path.assert_not_called()
                    self.assertEqual(store.file_path.read_bytes(), raw)
                    self.assertEqual(self.temp_paths(store), before_temps)

    def test_missing_target_is_empty_v2_state_and_accepts_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            self.assertFalse(store.file_path.exists())

            revision = store.save_revision(
                self.SYSTEM_ID,
                self.ENCLOSURE_ID,
                self.SLOT,
            )
            store.save_mapping(self.mapping("FIRST"), expected_revision=revision)

            payload = json.loads(store.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(
                [item.serial for item in store.list_mappings()],
                ["FIRST"],
            )

    def test_load_all_preserves_historical_malformed_v1_tolerance_without_writing(self) -> None:
        malformed_v1_documents = (
            b'{"version":1,"slot_mappings":{"legacy:7":{"slot":true}}}',
            b'{"version":1,"slot_mappings":',
        )
        for raw in malformed_v1_documents:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                store.file_path.write_bytes(raw)

                self.assertEqual(store.load_all(), {})
                self.assertEqual(store.file_path.read_bytes(), raw)
                self.assertEqual(self.temp_paths(store), set())


if __name__ == "__main__":
    unittest.main()
