from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.models.domain import ManualMapping, SlotView
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

    def test_preloaded_v1_entries_preserve_document_version_for_v2_prefixed_keys(
        self,
    ) -> None:
        cases = (
            (
                "v2",
                "enc:a",
                ManualMapping(
                    system_id="v2",
                    enclosure_id="enc:a",
                    slot=1,
                    serial="SCOPED",
                ),
                False,
            ),
            (
                "v2:[x]",
                "enc::a:part",
                ManualMapping(
                    system_id="v2:[x]",
                    enclosure_id="enc::a:part",
                    slot=2,
                    serial="COMPLEX",
                ),
                False,
            ),
            (
                "v2",
                "enc:a",
                ManualMapping(
                    system_id=None,
                    enclosure_id="enc:a",
                    slot=3,
                    serial="SCOPED-HISTORY",
                ),
                False,
            ),
            (
                "other-system",
                "v2:enc:a",
                ManualMapping(
                    system_id=None,
                    enclosure_id="v2:enc:a",
                    slot=4,
                    serial="LEGACY",
                ),
                True,
            ),
        )
        for system_id, enclosure_id, mapping, legacy_only in cases:
            with (
                self.subTest(serial=mapping.serial),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                key = (
                    store._slot_key(system_id, enclosure_id, mapping.slot)
                    if mapping.serial != "LEGACY"
                    else store._v1_legacy_keys(enclosure_id, mapping.slot)[0]
                )
                self.assertTrue(key.startswith("v2:"))
                before = self.write_document(store, 1, {key: mapping})

                direct = store.get_mapping(
                    system_id,
                    enclosure_id,
                    mapping.slot,
                    allow_legacy_fallback=True,
                )
                direct_legacy = store.has_legacy_only_mapping(
                    system_id,
                    enclosure_id,
                    mapping.slot,
                )
                direct_list = store.list_mappings(system_id, enclosure_id)
                loaded = store.load_all()
                preloaded = store.get_mapping(
                    system_id,
                    enclosure_id,
                    mapping.slot,
                    allow_legacy_fallback=True,
                    loaded_entries=loaded,
                )
                preloaded_legacy = store.has_legacy_only_mapping(
                    system_id,
                    enclosure_id,
                    mapping.slot,
                    loaded_entries=loaded,
                )

                self.assertEqual(preloaded, direct)
                self.assertEqual(preloaded_legacy, direct_legacy)
                self.assertEqual(preloaded_legacy, legacy_only)
                self.assertEqual(direct_list, [direct])
                self.assertEqual(loaded, {key: mapping})
                self.assertEqual(list(loaded), [key])
                self.assertEqual(store.file_path.read_bytes(), before)

    def test_loaded_entry_version_survives_empty_state_and_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            before = self.write_document(store, 1, {})
            loaded = store.load_all()

            self.assertEqual(loaded, {})
            self.assertIs(type(getattr(loaded, "store_version")), int)
            self.assertEqual(getattr(loaded, "store_version"), 1)
            with self.assertRaises(AttributeError):
                setattr(loaded, "store_version", 2)
            self.assertEqual(store._state_from_entries(loaded).version, 1)
            mapping = ManualMapping(
                system_id="v2",
                enclosure_id="enc:a",
                slot=1,
                serial="MUTATED",
            )
            loaded[store._slot_key("v2", "enc:a", 1)] = mapping

            self.assertEqual(store._state_from_entries(loaded).version, 1)
            self.assertEqual(
                store.get_mapping("v2", "enc:a", 1, loaded_entries=loaded),
                mapping,
            )
            self.assertEqual(store.file_path.read_bytes(), before)

    def test_plain_preloaded_mappings_keep_conservative_version_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            v1_mapping = ManualMapping(
                system_id="system-a", enclosure_id="enc-a", slot=1
            )
            v2_mapping = ManualMapping(
                system_id="system-b", enclosure_id="enc-b", slot=2
            )

            self.assertEqual(
                store.get_mapping(
                    "system-a",
                    "enc-a",
                    1,
                    loaded_entries={"system-a:enc-a:1": v1_mapping},
                ),
                v1_mapping,
            )
            self.assertEqual(
                store.get_mapping(
                    "system-b",
                    "enc-b",
                    2,
                    loaded_entries={store._encode_v2_key("system-b", "enc-b", 2): v2_mapping},
                ),
                v2_mapping,
            )
            with self.assertRaises(MappingScopeConflict):
                store.get_mapping(
                    "system-a",
                    "enc-a",
                    1,
                    loaded_entries={
                        "system-a:enc-a:1": v1_mapping,
                        store._encode_v2_key("system-b", "enc-b", 2): v2_mapping,
                    },
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

    def test_atomic_writer_uses_exclusive_shared_group_temp(self) -> None:
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
            self.assertEqual(observed, {"mode": 0o660})
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

    def test_write_temp_bytes_handles_partial_counts_and_rejects_no_progress(self) -> None:
        class PartialWriter:
            def __init__(self, counts: list[int | None]) -> None:
                self.counts = iter(counts)
                self.data = bytearray()

            def write(self, data: bytes) -> int | None:
                count = next(self.counts)
                if count is not None and count > 0:
                    self.data.extend(data[:count])
                return count

        payload = b"complete-payload"
        writer = PartialWriter([1, 2, 3, 4, 6])
        self.assertEqual(
            MappingStore._write_temp_bytes(writer, payload),  # type: ignore[arg-type]
            len(payload),
        )
        self.assertEqual(bytes(writer.data), payload)

        count_cases: tuple[list[int | None], ...] = ([2, 0], [2, None])
        for counts in count_cases:
            with self.subTest(counts=counts):
                stalled = PartialWriter(counts)
                with self.assertRaises(OSError):
                    MappingStore._write_temp_bytes(  # type: ignore[arg-type]
                        stalled, payload
                    )
                self.assertEqual(bytes(stalled.data), payload[:2])

    def test_incomplete_helper_result_never_replaces_target_or_leaves_temp(self) -> None:
        for target_exists in (True, False):
            for result in (1, 0, None):
                with (
                    self.subTest(target_exists=target_exists, result=result),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    store = self.make_store(temp_dir)
                    old = None
                    if target_exists:
                        old = self.write_document(
                            store,
                            1,
                            {
                                "system-a:enc-a:1": ManualMapping(
                                    system_id="system-a",
                                    enclosure_id="enc-a",
                                    slot=1,
                                    serial="OLD",
                                )
                            },
                        )

                    def incomplete(handle: object, data: bytes) -> int | None:
                        if result:
                            handle.write(data[:result])  # type: ignore[attr-defined]
                        return result

                    with (
                        patch.object(store, "_write_temp_bytes", side_effect=incomplete),
                        patch.object(
                            store,
                            "_replace_temp_file",
                            wraps=store._replace_temp_file,
                        ) as replace,
                        patch.object(
                            store,
                            "_flush_temp_file",
                            wraps=store._flush_temp_file,
                        ) as flush,
                        patch.object(
                            store,
                            "_fsync_temp_file",
                            wraps=store._fsync_temp_file,
                        ) as fsync,
                        self.assertRaises(OSError),
                    ):
                        store.save_mapping(
                            ManualMapping(
                                system_id="system-b",
                                enclosure_id="enc-b",
                                slot=2,
                                serial="NEW",
                            )
                        )

                    replace.assert_not_called()
                    flush.assert_not_called()
                    fsync.assert_not_called()
                    self.assertEqual(
                        list(store.file_path.parent.glob(f"{store.file_path.name}.*.tmp")),
                        [],
                    )
                    if old is None:
                        self.assertFalse(store.file_path.exists())
                        self.assertEqual(self.make_store(temp_dir).load_all(), {})
                    else:
                        self.assertEqual(store.file_path.read_bytes(), old)
                        restarted = self.make_store(temp_dir)
                        resolved = restarted.get_mapping("system-a", "enc-a", 1)
                        self.assertIsNotNone(resolved)
                        assert resolved is not None
                        self.assertEqual(resolved.serial, "OLD")

    def test_successive_partial_writes_publish_exact_canonical_v2_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            self.write_document(
                store,
                1,
                {
                    "system-a:enc-a:1": ManualMapping(
                        system_id="system-a",
                        enclosure_id="enc-a",
                        slot=1,
                        serial="OLD",
                    )
                },
            )
            expected: dict[str, bytes] = {}
            serialize = store._serialize_v2

            def capture(mappings: object) -> bytes:
                data = serialize(mappings)  # type: ignore[arg-type]
                expected["data"] = data
                return data

            def partial(handle: object, data: bytes) -> int:
                total = 0
                for size in (1, 2, 3, len(data)):
                    if total == len(data):
                        break
                    count = handle.write(data[total : total + size])  # type: ignore[attr-defined]
                    total += count
                return total

            with (
                patch.object(store, "_serialize_v2", side_effect=capture),
                patch.object(store, "_write_temp_bytes", side_effect=partial),
            ):
                store.save_mapping(
                    ManualMapping(
                        system_id="system-b",
                        enclosure_id="enc-b",
                        slot=2,
                        serial="NEW",
                    )
                )

            self.assertEqual(store.file_path.read_bytes(), expected["data"])
            self.assertEqual(
                {mapping.serial for mapping in store.load_all().values()},
                {"OLD", "NEW"},
            )

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

    def test_first_mapping_generation_is_readable_and_writable_by_the_shared_app_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)

            store.save_mapping(self.mapping())

            self.assertEqual(store.file_path.stat().st_mode & 0o777, 0o660)

    def test_mapping_replacement_upgrades_owner_only_mode_for_admin_backup_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            self.write_valid_document(store, 2)
            store.file_path.chmod(0o600)

            store.save_mapping(self.mapping("NEW"))

            self.assertEqual(store.file_path.stat().st_mode & 0o777, 0o660)

    def test_initial_source_read_errors_fail_closed_for_every_authority_surface(self) -> None:
        original_read_bytes = Path.read_bytes
        original_lstat = os.lstat
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
                        def fail_initial_target_observation(path: Path) -> os.stat_result:
                            if Path(path) == store.file_path:
                                raise failure
                            return original_lstat(path)

                        with (
                            patch.object(
                                mapping_store_module.os,
                                "lstat",
                                fail_initial_target_observation,
                            ),
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

    def test_existing_target_open_and_read_losses_fail_every_authority_surface(self) -> None:
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
        failures = (
            ("open_missing", FileNotFoundError),
            ("open_denied", PermissionError),
            ("open_error", OSError),
            ("read_missing", FileNotFoundError),
            ("read_denied", PermissionError),
            ("read_error", OSError),
        )
        original_open = os.open
        original_read = os.read
        for version in (1, 2):
            for failure_name, failure_type in failures:
                for operation in operations:
                    with (
                        self.subTest(
                            version=version,
                            failure=failure_name,
                            operation=operation,
                        ),
                        tempfile.TemporaryDirectory() as temp_dir,
                    ):
                        store = self.make_store(temp_dir)
                        before = self.write_valid_document(store, version)
                        source_descriptor: int | None = None
                        import_preview = None
                        if operation == "import":
                            import_preview = store.preview_replace_mappings(
                                self.SYSTEM_ID,
                                self.ENCLOSURE_ID,
                                [self.mapping("NEW")],
                            )

                        def guarded_open(path: Path, flags: int, mode: int = 0o777) -> int:
                            nonlocal source_descriptor
                            if Path(path) == store.file_path and not flags & os.O_CREAT:
                                if failure_name.startswith("open_"):
                                    raise failure_type("synthetic source open loss")
                                source_descriptor = original_open(path, flags, mode)
                                return source_descriptor
                            return original_open(path, flags, mode)

                        def guarded_read(descriptor: int, size: int) -> bytes:
                            if descriptor == source_descriptor:
                                raise failure_type("synthetic source read loss")
                            return original_read(descriptor, size)

                        with (
                            patch.object(mapping_store_module.os, "open", guarded_open),
                            patch.object(mapping_store_module.os, "read", guarded_read),
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
                            self.assertRaises(failure_type),
                        ):
                            if operation == "import":
                                assert import_preview is not None
                                store.apply_mapping_import(
                                    self.SYSTEM_ID,
                                    self.ENCLOSURE_ID,
                                    [self.mapping("NEW")],
                                    expected_revision=import_preview["revision"],
                                    import_digest=import_preview["import_digest"],
                                )
                            else:
                                self.authority_surface(store, operation)

                        create_temp.assert_not_called()
                        replace_temp.assert_not_called()
                        self.assertEqual(store.file_path.read_bytes(), before)
                        self.assertEqual(self.temp_paths(store), set())
                        if source_descriptor is not None:
                            with self.assertRaises(OSError):
                                os.fstat(source_descriptor)

    def test_target_swap_between_observation_and_open_fails_before_mutation(self) -> None:
        original_open = os.open
        original_replace = os.replace
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                self.write_valid_document(store, version)
                replacement = store.file_path.with_name("replacement.json")
                replacement_bytes = self.write_valid_document(
                    MappingStore(replacement),
                    version,
                ).replace(b"OLD", b"NEW")
                replacement.write_bytes(replacement_bytes)
                swapped = False

                def swap_then_open(path: Path, flags: int, mode: int = 0o777) -> int:
                    nonlocal swapped
                    if Path(path) == store.file_path and not flags & os.O_CREAT and not swapped:
                        swapped = True
                        original_replace(replacement, store.file_path)
                    return original_open(path, flags, mode)

                with (
                    patch.object(mapping_store_module.os, "open", swap_then_open),
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
                    self.assertRaises(OSError),
                ):
                    store.save_mapping(self.mapping("CALLER"))

                self.assertTrue(swapped)
                create_temp.assert_not_called()
                replace_temp.assert_not_called()
                self.assertEqual(store.file_path.read_bytes(), replacement_bytes)
                self.assertEqual(self.temp_paths(store), set())

    def test_opened_descriptor_identity_mismatch_fails_before_mutation(self) -> None:
        original_open = os.open
        original_fstat = os.fstat
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            before = self.write_valid_document(store, 1)
            source_descriptor: int | None = None

            def track_open(path: Path, flags: int, mode: int = 0o777) -> int:
                nonlocal source_descriptor
                descriptor = original_open(path, flags, mode)
                if Path(path) == store.file_path and not flags & os.O_CREAT:
                    source_descriptor = descriptor
                return descriptor

            def mismatched_fstat(descriptor: int) -> os.stat_result:
                result = original_fstat(descriptor)
                if descriptor != source_descriptor:
                    return result
                values = list(result)
                values[1] += 1
                return os.stat_result(values)

            with (
                patch.object(mapping_store_module.os, "open", track_open),
                patch.object(mapping_store_module.os, "fstat", mismatched_fstat),
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
                self.assertRaises(OSError),
            ):
                store.save_mapping(self.mapping("NEW"))

            create_temp.assert_not_called()
            replace_temp.assert_not_called()
            self.assertEqual(store.file_path.read_bytes(), before)
            self.assertEqual(self.temp_paths(store), set())
            assert source_descriptor is not None
            with self.assertRaises(OSError):
                os.fstat(source_descriptor)

    def test_configured_source_rejects_symlinks_and_non_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_store = MappingStore(root / "real.json")
            real_bytes = self.write_valid_document(real_store, 1)
            symlink_store = MappingStore(root / "symlink.json")
            symlink_store.file_path.symlink_to(real_store.file_path)

            with (
                patch.object(
                    symlink_store,
                    "_create_temp_file",
                    wraps=symlink_store._create_temp_file,
                ) as create_temp,
                self.assertRaises(OSError),
            ):
                symlink_store.save_mapping(self.mapping("NEW"))

            create_temp.assert_not_called()
            self.assertTrue(symlink_store.file_path.is_symlink())
            self.assertEqual(real_store.file_path.read_bytes(), real_bytes)
            self.assertEqual(self.temp_paths(symlink_store), set())

            directory_store = MappingStore(root / "configured-directory")
            directory_store.file_path.mkdir()
            with self.assertRaises(OSError):
                directory_store.save_revision(
                    self.SYSTEM_ID,
                    self.ENCLOSURE_ID,
                    self.SLOT,
                )

    def test_saves_publish_exact_shared_group_mode_under_every_umask(self) -> None:
        original_replace = os.replace
        for requested_umask in (0o000, 0o077, 0o777):
            with (
                self.subTest(umask=oct(requested_umask)),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                provisional_modes: list[int] = []

                def inspect_then_replace(source: Path, target: Path) -> None:
                    provisional_modes.append(os.stat(source).st_mode & 0o777)
                    original_replace(source, target)

                previous_umask = os.umask(requested_umask)
                try:
                    with patch.object(mapping_store_module.os, "replace", inspect_then_replace):
                        try:
                            store.save_mapping(self.mapping("FIRST"))
                        except Exception as exc:
                            self.fail(
                                f"save failed under umask {oct(requested_umask)}: "
                                f"{type(exc).__name__}"
                            )
                finally:
                    os.umask(previous_umask)

                self.assertEqual(provisional_modes, [0o660])
                self.assertEqual(os.stat(store.file_path).st_mode & 0o777, 0o660)
                resolved = store.get_mapping(
                    self.SYSTEM_ID,
                    self.ENCLOSURE_ID,
                    self.SLOT,
                )
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved.serial, "FIRST")

    def test_fchmod_failure_closes_and_unlinks_temp_without_replacement(self) -> None:
        for target_exists in (False, True):
            with (
                self.subTest(target_exists=target_exists),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                before = self.write_valid_document(store, 1) if target_exists else None
                temp_descriptor: int | None = None

                def fail_fchmod(descriptor: int, mode: int) -> None:
                    nonlocal temp_descriptor
                    temp_descriptor = descriptor
                    raise OSError("synthetic fchmod failure")

                with (
                    patch.object(mapping_store_module.os, "fchmod", fail_fchmod),
                    patch.object(
                        store,
                        "_replace_temp_file",
                        wraps=store._replace_temp_file,
                    ) as replace_temp,
                    self.assertRaises(OSError),
                ):
                    store.save_mapping(self.mapping("NEW"))

                replace_temp.assert_not_called()
                self.assertEqual(self.temp_paths(store), set())
                if before is None:
                    self.assertFalse(store.file_path.exists())
                else:
                    self.assertEqual(store.file_path.read_bytes(), before)
                assert temp_descriptor is not None
                with self.assertRaises(OSError):
                    os.fstat(temp_descriptor)

    def test_wrong_post_fchmod_mode_closes_and_unlinks_temp_without_replacement(self) -> None:
        original_fchmod = os.fchmod
        for target_exists in (False, True):
            with (
                self.subTest(target_exists=target_exists),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                before = self.write_valid_document(store, 1) if target_exists else None
                temp_descriptor: int | None = None

                def leave_wrong_mode(descriptor: int, mode: int) -> None:
                    nonlocal temp_descriptor
                    temp_descriptor = descriptor
                    original_fchmod(descriptor, 0o400)

                with (
                    patch.object(mapping_store_module.os, "fchmod", leave_wrong_mode),
                    patch.object(
                        store,
                        "_replace_temp_file",
                        wraps=store._replace_temp_file,
                    ) as replace_temp,
                    self.assertRaises(OSError),
                ):
                    store.save_mapping(self.mapping("NEW"))

                replace_temp.assert_not_called()
                self.assertEqual(self.temp_paths(store), set())
                if before is None:
                    self.assertFalse(store.file_path.exists())
                else:
                    self.assertEqual(store.file_path.read_bytes(), before)
                assert temp_descriptor is not None
                with self.assertRaises(OSError):
                    os.fstat(temp_descriptor)

    def test_load_all_rejects_explicit_non_v1_versions_before_shape_tolerance(self) -> None:
        malformed_documents = (
            b'{"version":2}',
            b'{"version":2,"slot_mappings":[]}',
            b'{"version":3,"slot_mappings":[]}',
            b'{"version":true,"slot_mappings":[]}',
        )
        for raw in malformed_documents:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                store.file_path.write_bytes(raw)

                with self.assertRaises(MappingScopeConflict):
                    store.load_all()
                self.assertEqual(store.file_path.read_bytes(), raw)
                self.assertEqual(self.temp_paths(store), set())

    def test_load_all_preserves_historical_malformed_v1_tolerance_without_writing(self) -> None:
        malformed_v1_documents = (
            b'{"version":1}',
            b'{"version":1,"slot_mappings":[]}',
            b'{}',
            b'{"slot_mappings":[]}',
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

    def test_load_all_rejects_duplicate_keys_without_writing(self) -> None:
        raw = b'{"version":1,"version":1,"slot_mappings":{}}'
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            store.file_path.write_bytes(raw)

            with self.assertRaises(MappingScopeConflict):
                store.load_all()
            self.assertEqual(store.file_path.read_bytes(), raw)
            self.assertEqual(self.temp_paths(store), set())


class MappingStoreIdentityBoundTempCleanupTests(unittest.TestCase):
    def make_store(self, root: str) -> MappingStore:
        return MappingStore(str(Path(root) / "mappings.json"))

    @staticmethod
    def old_mapping() -> ManualMapping:
        return ManualMapping(
            system_id="system-a",
            enclosure_id="enc-a",
            slot=1,
            serial="OLD",
        )

    @staticmethod
    def new_mapping() -> ManualMapping:
        return ManualMapping(
            system_id="system-b",
            enclosure_id="enc-b",
            slot=2,
            serial="NEW",
        )

    def seed_target(self, store: MappingStore) -> bytes:
        mapping = self.old_mapping()
        payload = {
            "version": 1,
            "updated_at": "2026-09-05T00:00:00+00:00",
            "slot_mappings": {
                "system-a:enc-a:1": mapping.model_dump(mode="json"),
            },
        }
        raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        store.file_path.write_bytes(raw)
        return raw

    def assert_target_unchanged(
        self,
        store: MappingStore,
        target_exists: bool,
        old: bytes | None,
    ) -> None:
        if target_exists:
            self.assertEqual(store.file_path.read_bytes(), old)
        else:
            self.assertFalse(store.file_path.exists())

    def test_pre_replace_cleanup_never_removes_swapped_pathnames(self) -> None:
        failure_points = (
            "_write_temp_file",
            "_write_temp_bytes",
            "_flush_temp_file",
            "_fsync_temp_file",
            "descriptor_close",
        )
        replacement_kinds = ("regular", "symlink", "directory", "missing")
        original_close = os.close

        for target_exists in (False, True):
            for replacement_kind in replacement_kinds:
                for failure_point in failure_points:
                    with (
                        self.subTest(
                            target_exists=target_exists,
                            replacement_kind=replacement_kind,
                            failure_point=failure_point,
                        ),
                        tempfile.TemporaryDirectory() as temp_dir,
                    ):
                        store = self.make_store(temp_dir)
                        old = self.seed_target(store) if target_exists else None
                        original_create = store._create_temp_file
                        created_path: Path | None = None
                        created_descriptor: int | None = None
                        symlink_target: Path | None = None

                        def create_then_swap() -> Any:
                            nonlocal created_path, created_descriptor, symlink_target
                            result = original_create()
                            created_path = result[0]
                            created_descriptor = result[1]
                            created_path.unlink()
                            if replacement_kind == "regular":
                                created_path.write_bytes(b"unrelated replacement")
                            elif replacement_kind == "symlink":
                                symlink_target = created_path.with_name(
                                    "unrelated-evidence"
                                )
                                symlink_target.write_bytes(b"unrelated symlink target")
                                created_path.symlink_to(symlink_target)
                            elif replacement_kind == "directory":
                                created_path.mkdir()
                                (created_path / "evidence").write_bytes(
                                    b"unrelated directory"
                                )
                            return result

                        def close_then_fail(descriptor: int) -> None:
                            if descriptor != created_descriptor:
                                original_close(descriptor)
                                return
                            original_close(descriptor)
                            raise OSError("synthetic descriptor close failure")

                        with ExitStack() as stack:
                            stack.enter_context(
                                patch.object(
                                    store,
                                    "_create_temp_file",
                                    side_effect=create_then_swap,
                                )
                            )
                            replace_temp = stack.enter_context(
                                patch.object(
                                    store,
                                    "_replace_temp_file",
                                    wraps=store._replace_temp_file,
                                )
                            )
                            if failure_point == "descriptor_close":
                                stack.enter_context(
                                    patch.object(
                                        mapping_store_module.os,
                                        "close",
                                        close_then_fail,
                                    )
                                )
                            else:
                                stack.enter_context(
                                    patch.object(
                                        store,
                                        failure_point,
                                        side_effect=OSError(
                                            f"synthetic {failure_point} failure"
                                        ),
                                    )
                                )
                            with self.assertRaises(OSError):
                                store.save_mapping(self.new_mapping())

                        replace_temp.assert_not_called()
                        assert created_path is not None
                        assert created_descriptor is not None
                        with self.assertRaises(OSError):
                            os.fstat(created_descriptor)
                        if replacement_kind == "regular":
                            self.assertEqual(
                                created_path.read_bytes(), b"unrelated replacement"
                            )
                        elif replacement_kind == "symlink":
                            self.assertTrue(created_path.is_symlink())
                            self.assertEqual(
                                created_path.read_bytes(), b"unrelated symlink target"
                            )
                            assert symlink_target is not None
                            self.assertEqual(
                                symlink_target.read_bytes(), b"unrelated symlink target"
                            )
                        elif replacement_kind == "directory":
                            self.assertTrue(created_path.is_dir())
                            self.assertEqual(
                                (created_path / "evidence").read_bytes(),
                                b"unrelated directory",
                            )
                        else:
                            self.assertFalse(created_path.exists())
                        self.assert_target_unchanged(
                            store, target_exists=target_exists, old=old
                        )

    def test_pre_replace_cleanup_removes_the_exact_owned_temp_inode(self) -> None:
        failure_points = (
            "_write_temp_file",
            "_write_temp_bytes",
            "_flush_temp_file",
            "_fsync_temp_file",
            "descriptor_close",
        )
        original_close = os.close

        for failure_point in failure_points:
            with (
                self.subTest(failure_point=failure_point),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                original_create = store._create_temp_file
                created_path: Path | None = None
                created_descriptor: int | None = None

                def track_create() -> Any:
                    nonlocal created_path, created_descriptor
                    result = original_create()
                    created_path = result[0]
                    created_descriptor = result[1]
                    return result

                def close_then_fail(descriptor: int) -> None:
                    if descriptor != created_descriptor:
                        original_close(descriptor)
                        return
                    original_close(descriptor)
                    raise OSError("synthetic descriptor close failure")

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            store, "_create_temp_file", side_effect=track_create
                        )
                    )
                    replace_temp = stack.enter_context(
                        patch.object(
                            store,
                            "_replace_temp_file",
                            wraps=store._replace_temp_file,
                        )
                    )
                    if failure_point == "descriptor_close":
                        stack.enter_context(
                            patch.object(
                                mapping_store_module.os,
                                "close",
                                close_then_fail,
                            )
                        )
                    else:
                        stack.enter_context(
                            patch.object(
                                store,
                                failure_point,
                                side_effect=OSError(
                                    f"synthetic {failure_point} failure"
                                ),
                            )
                        )
                    with self.assertRaises(OSError):
                        store.save_mapping(self.new_mapping())

                replace_temp.assert_not_called()
                assert created_path is not None
                assert created_descriptor is not None
                self.assertFalse(created_path.exists())
                with self.assertRaises(OSError):
                    os.fstat(created_descriptor)

    def test_cleanup_does_not_search_for_a_renamed_owned_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            original_create = store._create_temp_file
            original_path: Path | None = None
            evidence_path = store.file_path.with_name("renamed-operation-evidence")
            descriptor: int | None = None

            def create_then_rename() -> Any:
                nonlocal original_path, descriptor
                result = original_create()
                original_path = result[0]
                descriptor = result[1]
                original_path.rename(evidence_path)
                return result

            with (
                patch.object(
                    store, "_create_temp_file", side_effect=create_then_rename
                ),
                patch.object(
                    store,
                    "_write_temp_file",
                    side_effect=OSError("synthetic write failure"),
                ),
                patch.object(
                    store,
                    "_replace_temp_file",
                    wraps=store._replace_temp_file,
                ) as replace_temp,
                self.assertRaises(OSError),
            ):
                store.save_mapping(self.new_mapping())

            replace_temp.assert_not_called()
            assert original_path is not None
            assert descriptor is not None
            self.assertFalse(original_path.exists())
            self.assertTrue(evidence_path.is_file())
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_cleanup_may_unlink_a_recreated_hardlink_to_the_exact_owned_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            original_create = store._create_temp_file
            original_path: Path | None = None
            evidence_path = store.file_path.with_name("hardlinked-operation-evidence")

            def create_then_relink() -> Any:
                nonlocal original_path
                result = original_create()
                original_path = result[0]
                original_path.rename(evidence_path)
                os.link(evidence_path, original_path)
                return result

            with (
                patch.object(
                    store, "_create_temp_file", side_effect=create_then_relink
                ),
                patch.object(
                    store,
                    "_write_temp_file",
                    side_effect=OSError("synthetic write failure"),
                ),
                self.assertRaises(OSError),
            ):
                store.save_mapping(self.new_mapping())

            assert original_path is not None
            self.assertFalse(original_path.exists())
            self.assertTrue(evidence_path.is_file())

    def test_cleanup_lstat_failures_and_identity_mismatches_preserve_path(self) -> None:
        original_lstat = os.lstat
        cases = (
            ("missing", FileNotFoundError("synthetic lstat missing")),
            ("denied", PermissionError("synthetic lstat denied")),
            ("error", OSError("synthetic lstat failure")),
            ("wrong_type", None),
            ("wrong_device", None),
            ("wrong_inode", None),
        )
        for case, lstat_failure in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                original_create = store._create_temp_file
                created_path: Path | None = None
                cleanup_armed = False

                def track_create() -> Any:
                    nonlocal created_path, cleanup_armed
                    result = original_create()
                    created_path = result[0]
                    cleanup_armed = True
                    return result

                def injected_lstat(path: Path) -> os.stat_result:
                    if cleanup_armed and Path(path) == created_path:
                        if lstat_failure is not None:
                            raise lstat_failure
                        result = original_lstat(path)
                        values = list(result)
                        if case == "wrong_type":
                            values[0] = (
                                (int(values[0]) & 0o7777)
                                | mapping_store_module.stat.S_IFLNK
                            )
                        elif case == "wrong_device":
                            values[2] += 1
                        else:
                            values[1] += 1
                        return os.stat_result(values)
                    return original_lstat(path)

                original_failure = OSError("synthetic write failure")
                with (
                    patch.object(
                        store, "_create_temp_file", side_effect=track_create
                    ),
                    patch.object(
                        store, "_write_temp_file", side_effect=original_failure
                    ),
                    patch.object(mapping_store_module.os, "lstat", injected_lstat),
                    self.assertRaises(OSError) as raised,
                ):
                    store.save_mapping(self.new_mapping())

                self.assertIs(raised.exception, original_failure)
                assert created_path is not None
                self.assertTrue(created_path.exists())

    def test_cleanup_identity_allows_permission_mode_change_on_owned_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            original_create = store._create_temp_file
            created_path: Path | None = None

            def create_then_chmod() -> Any:
                nonlocal created_path
                result = original_create()
                created_path = result[0]
                created_path.chmod(0o400)
                return result

            with (
                patch.object(
                    store, "_create_temp_file", side_effect=create_then_chmod
                ),
                patch.object(
                    store,
                    "_write_temp_file",
                    side_effect=OSError("synthetic write failure"),
                ),
                self.assertRaises(OSError),
            ):
                store.save_mapping(self.new_mapping())

            assert created_path is not None
            self.assertFalse(created_path.exists())

    def test_cleanup_unlink_errors_preserve_original_failure_and_temp_evidence(
        self,
    ) -> None:
        original_unlink = Path.unlink
        for cleanup_failure in (
            FileNotFoundError("synthetic unlink missing"),
            PermissionError("synthetic unlink denied"),
            OSError("synthetic unlink failure"),
        ):
            with (
                self.subTest(cleanup_failure=type(cleanup_failure).__name__),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                original_create = store._create_temp_file
                created_path: Path | None = None

                def track_create() -> Any:
                    nonlocal created_path
                    result = original_create()
                    created_path = result[0]
                    return result

                def fail_owned_unlink(
                    path: Path, missing_ok: bool = False
                ) -> None:
                    if Path(path) == created_path:
                        raise cleanup_failure
                    original_unlink(path, missing_ok=missing_ok)

                original_failure = OSError("synthetic write failure")
                with (
                    patch.object(
                        store, "_create_temp_file", side_effect=track_create
                    ),
                    patch.object(
                        store, "_write_temp_file", side_effect=original_failure
                    ),
                    patch.object(Path, "unlink", fail_owned_unlink),
                    self.assertRaises(OSError) as raised,
                ):
                    store.save_mapping(self.new_mapping())

                self.assertIs(raised.exception, original_failure)
                assert created_path is not None
                self.assertTrue(created_path.exists())

    def test_fchmod_failure_cleanup_preserves_a_swapped_unrelated_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            swapped_path: Path | None = None
            descriptor: int | None = None

            def swap_then_fail_fchmod(temp_descriptor: int, mode: int) -> None:
                nonlocal swapped_path, descriptor
                descriptor = temp_descriptor
                paths = list(
                    store.file_path.parent.glob(f"{store.file_path.name}.*.tmp")
                )
                self.assertEqual(len(paths), 1)
                swapped_path = paths[0]
                swapped_path.unlink()
                swapped_path.write_bytes(b"unrelated replacement")
                raise OSError("synthetic fchmod failure")

            with (
                patch.object(
                    mapping_store_module.os,
                    "fchmod",
                    swap_then_fail_fchmod,
                ),
                patch.object(
                    store,
                    "_replace_temp_file",
                    wraps=store._replace_temp_file,
                ) as replace_temp,
                self.assertRaises(OSError),
            ):
                store.save_mapping(self.new_mapping())

            replace_temp.assert_not_called()
            assert swapped_path is not None
            assert descriptor is not None
            self.assertEqual(swapped_path.read_bytes(), b"unrelated replacement")
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_fstat_mode_proof_failure_cleans_only_with_recovered_identity(
        self,
    ) -> None:
        original_fstat = os.fstat
        for scenario in ("unavailable", "recovered", "wrong_type"):
            with (
                self.subTest(scenario=scenario),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                descriptor: int | None = None
                calls = 0

                def injected_fstat(temp_descriptor: int) -> os.stat_result:
                    nonlocal descriptor, calls
                    descriptor = temp_descriptor
                    calls += 1
                    if scenario == "unavailable" or (
                        scenario == "recovered" and calls == 1
                    ):
                        raise OSError("synthetic fstat failure")
                    result = original_fstat(temp_descriptor)
                    if scenario == "wrong_type":
                        values = list(result)
                        values[0] = (
                            (int(values[0]) & 0o7777)
                            | mapping_store_module.stat.S_IFLNK
                        )
                        return os.stat_result(values)
                    return result

                with (
                    patch.object(mapping_store_module.os, "fstat", injected_fstat),
                    patch.object(
                        store,
                        "_replace_temp_file",
                        wraps=store._replace_temp_file,
                    ) as replace_temp,
                    self.assertRaises(OSError),
                ):
                    store.save_mapping(self.new_mapping())

                replace_temp.assert_not_called()
                remaining = list(
                    store.file_path.parent.glob(f"{store.file_path.name}.*.tmp")
                )
                self.assertEqual(len(remaining), 0 if scenario == "recovered" else 1)
                assert descriptor is not None
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_post_replace_failures_never_attempt_temp_cleanup(self) -> None:
        for failure_point in (
            "_fsync_parent_directory",
            "_validate_published_bytes",
        ):
            with (
                self.subTest(failure_point=failure_point),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                store = self.make_store(temp_dir)
                self.seed_target(store)
                with (
                    patch.object(
                        store,
                        failure_point,
                        side_effect=OSError(f"synthetic {failure_point} failure"),
                    ),
                    patch.object(
                        Path,
                        "unlink",
                        side_effect=AssertionError(
                            "post-replace cleanup was attempted"
                        ),
                    ) as unlink,
                    self.assertRaises(mapping_store_module.MappingDurabilityError),
                ):
                    store.save_mapping(self.new_mapping())

                unlink.assert_not_called()
                payload = json.loads(store.file_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["version"], 2)
                self.assertEqual(
                    {mapping.serial for mapping in store.load_all().values()},
                    {"OLD", "NEW"},
                )


BATCH_SYSTEM = "synthetic-system-a"
BATCH_SHELF = "synthetic-shelf-a"
BATCH_DRAWER = f"{BATCH_SHELF}::dell-md1280-drawer-top-42"


def batch_mapping(
    system: str | None = BATCH_SYSTEM, enclosure: str | None = BATCH_SHELF,
    slot: int = 0, serial: str = "SYNTHETIC",
) -> ManualMapping:
    return ManualMapping(
        system_id=system, enclosure_id=enclosure, slot=slot, serial=serial,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def write_batch_document(store, version, rows):
    store.file_path.write_text(json.dumps({
        "version": version,
        "slot_mappings": {key: row.model_dump(mode="json") for key, row in rows.items()},
    }), encoding="utf-8")


def batch_parity_rows(store, version):
    models = [
        batch_mapping(), batch_mapping(enclosure=BATCH_DRAWER, slot=1),
        batch_mapping(system=None, slot=2), batch_mapping(enclosure=None, slot=3),
        batch_mapping(system=None, enclosure=None, slot=4),
        batch_mapping(system="synthetic-system-b", slot=0),
        batch_mapping(enclosure=f"{BATCH_SHELF}:neighbor", slot=0),
    ]
    if version == 2:
        models = [store._canonical_mapping(row) for row in models]
        return {store._encode_v2_key(row.system_id, row.enclosure_id, row.slot): row
                for row in models}
    return {(f"{row.enclosure_id or 'default'}:{row.slot}" if row.system_id is None
             else store._slot_key(row.system_id, row.enclosure_id, row.slot)): row
            for row in models}


class MappingRevisionBatchTests(unittest.TestCase):
    def test_attachment_reads_and_classifies_once_per_batch(self):
        from app.config import SystemConfig
        from app.services.inventory import InventoryService

        for count in (60, 347):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as root:
                store = MappingStore(str(Path(root) / "mapping.json"))
                rows = batch_parity_rows(store, 1)
                write_batch_document(store, 1, rows)
                before = store.file_path.read_bytes()
                service = InventoryService.__new__(InventoryService)
                service.mapping_store = store
                service.system = SystemConfig(id=BATCH_SYSTEM)
                slots = [SlotView(slot=i, slot_label=f"Bay {i}", row_index=0,
                                  column_index=i, enclosure_id=BATCH_DRAWER) for i in range(count)]
                expected_save = store.save_revisions(BATCH_SYSTEM, [(BATCH_SHELF, i) for i in range(count)])
                expected_clear = store.clear_revisions(BATCH_SYSTEM, [(BATCH_SHELF, i) for i in range(count)])
                with patch.object(store, "_read_document", wraps=store._read_document) as reads, \
                     patch.object(store, "_classify_row", wraps=store._classify_row) as classifications:
                    result = InventoryService._attach_mapping_revisions(service, slots)
                self.assertEqual(len(result), count)
                self.assertEqual([s.mapping_revision for s in result], list(expected_save.values()))
                self.assertEqual([s.mapping_clear_revision for s in result], list(expected_clear.values()))
                self.assertEqual(store.file_path.read_bytes(), before)
                self.assertLessEqual(reads.call_count, 2)
                self.assertLessEqual(classifications.call_count, 2 * len(rows))

    def test_batch_tokens_match_pre_optimization_bytes(self):
        # Fingerprints captured on a98917a before changing production code.
        expected = {
            1: "899bcb814d7d8b198dd331f29c479b2db4f8f7bf5415df75d9f2f4b6d8d6016e",
            2: "84f6ec1948baea449762f584009e149c27c2dc0f7ecadb792659f5b910e81e35",
        }
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as root:
                store = MappingStore(str(Path(root) / "mapping.json"))
                write_batch_document(store, version, batch_parity_rows(store, version))
                before = store.file_path.read_bytes()
                targets = [(scope, slot) for scope in (BATCH_SHELF, BATCH_DRAWER, None, f"{BATCH_SHELF}:neighbor")
                           for slot in range(6)]
                tokens = []
                for system in (BATCH_SYSTEM, None, "synthetic-system-b"):
                    for operation in ("save", "clear"):
                        batch = getattr(store, f"{operation}_revisions")(system, targets + targets[:1])
                        self.assertEqual(list(batch), targets)
                        singles = {target: getattr(store, f"{operation}_revision")(system, *target)
                                   for target in targets}
                        self.assertEqual(batch, singles)
                        tokens.extend(batch.values())
                fingerprint = hashlib.sha256(json.dumps(tokens).encode()).hexdigest()
                self.assertEqual(fingerprint, expected[version])
                self.assertEqual(store.file_path.read_bytes(), before)

    def test_conflict_relevance_is_target_specific(self):
        for kind in ("invalid-v1", "invalid-v2", "duplicate", "legacy-drawer"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                store = MappingStore(str(Path(root) / "mapping.json"))
                version = 2 if kind == "invalid-v2" else 1
                if kind.startswith("invalid"):
                    key = (store._encode_v2_key(BATCH_SYSTEM, BATCH_SHELF, 7) if version == 2
                           else store._slot_key(BATCH_SYSTEM, BATCH_SHELF, 7))
                    rows = {key: batch_mapping(enclosure=f"{BATCH_SHELF}:neighbor", slot=7)}
                else:
                    rows = {store._slot_key(BATCH_SYSTEM, BATCH_SHELF, 7): batch_mapping(slot=7),
                            (f"{BATCH_DRAWER}:7" if kind == "legacy-drawer"
                             else f"{BATCH_SYSTEM}:{BATCH_DRAWER}:7"):
                            batch_mapping(system=None if kind == "legacy-drawer" else BATCH_SYSTEM,
                                    enclosure=BATCH_DRAWER, slot=7, serial="DIVERGENT")}
                write_batch_document(store, version, rows)
                before = store.file_path.read_bytes()
                for operation in ("save", "clear"):
                    batch = getattr(store, f"{operation}_revisions")
                    single = getattr(store, f"{operation}_revision")
                    unrelated = [(BATCH_SHELF, 0), (f"{BATCH_SHELF}:other", 7), (BATCH_DRAWER, 8)]
                    self.assertEqual(batch(BATCH_SYSTEM, unrelated),
                                     {target: single(BATCH_SYSTEM, *target) for target in unrelated})
                    for targets in ([(BATCH_DRAWER, 7)], unrelated + [(BATCH_SHELF, 7)], [(BATCH_SHELF, 7)] + unrelated):
                        with self.assertRaises(MappingScopeConflict):
                            batch(BATCH_SYSTEM, targets)
                    self.assertEqual(batch("synthetic-system-c", [(BATCH_SHELF, 0)]),
                                     {(BATCH_SHELF, 0): single("synthetic-system-c", BATCH_SHELF, 0)})
                self.assertEqual(store.file_path.read_bytes(), before)

    def test_batches_refresh_after_mutation_and_preserve_cas_migration(self):
        with tempfile.TemporaryDirectory() as root:
            store = MappingStore(str(Path(root) / "mapping.json"))
            write_batch_document(store, 1, batch_parity_rows(store, 1))
            target = (BATCH_DRAWER, 1)
            save = store.save_revisions(BATCH_SYSTEM, [target])[target]
            clear = store.clear_revisions(BATCH_SYSTEM, [target])[target]
            store.save_mapping(batch_mapping(enclosure=BATCH_DRAWER, slot=1, serial="REPLACEMENT"),
                               expected_revision=save)
            self.assertEqual(json.loads(store.file_path.read_text())["version"], 2)
            fresh_save = store.save_revisions(BATCH_SYSTEM, [target])[target]
            fresh_clear = store.clear_revisions(BATCH_SYSTEM, [target])[target]
            self.assertNotEqual(save, fresh_save)
            self.assertNotEqual(clear, fresh_clear)
            with self.assertRaises(MappingRevisionConflict):
                store.save_mapping(batch_mapping(enclosure=BATCH_DRAWER, slot=1), expected_revision=save)
            with self.assertRaises(MappingRevisionConflict):
                store.clear_mapping(BATCH_SYSTEM, *target, expected_revision=clear)
            self.assertTrue(store.clear_mapping(BATCH_SYSTEM, *target, expected_revision=fresh_clear))

    def test_empty_batches_do_not_read_or_validate_document(self):
        with tempfile.TemporaryDirectory() as root:
            store = MappingStore(str(Path(root) / "mapping.json"))
            store.file_path.write_text("invalid JSON", encoding="utf-8")
            with patch.object(store, "_read_document", wraps=store._read_document) as reads:
                self.assertEqual(store.save_revisions(BATCH_SYSTEM, []), {})
                self.assertEqual(store.clear_revisions(BATCH_SYSTEM, []), {})
            self.assertEqual(reads.call_count, 0)


if __name__ == "__main__":
    unittest.main()
