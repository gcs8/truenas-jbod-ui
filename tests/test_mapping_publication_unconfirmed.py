"""A mapping save that published but could not confirm the path says only that.

After the atomic replacement the save reads the mapping path back. When an
outside process removes the path, puts a directory there or writes another
document before that readback, the update was published but the path no longer
confirms it. The error says so, without claiming the update is visible, and the
save leaves the outside content in place. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import main as app_main
from app.models.domain import ManualMapping
from app.services.mapping_store import MappingDurabilityError, MappingStore

OUTSIDE_DOCUMENT = (
    json.dumps(
        {
            "version": 2,
            "slot_mappings": {},
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        sort_keys=True,
    )
    + "\n"
).encode("utf-8")


def mapping(slot: int, notes: str) -> ManualMapping:
    return ManualMapping(system_id="sys-a", enclosure_id="shelf-a", slot=slot, notes=notes)


def seeded_store(directory: str) -> tuple[Path, MappingStore]:
    path = Path(directory) / "slot_mappings.json"
    store = MappingStore(path)
    store.save_mapping(mapping(1, "seed"))
    return path, store


def replace_path(path: Path, kind: str) -> None:
    """An outside change to the mapping path, outside the writers' protocol."""
    path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "other_document":
        path.write_bytes(OUTSIDE_DOCUMENT)


def outside_content(path: Path, kind: str):
    if kind == "removed":
        return path.exists()
    if kind == "directory":
        return path.is_dir()
    return path.read_bytes()


EXPECTED_OUTSIDE = {"removed": False, "directory": True, "other_document": OUTSIDE_DOCUMENT}


def assert_no_visibility_claim(test: unittest.TestCase, detail: str) -> None:
    lowered = detail.lower()
    test.assertNotIn("is visible", lowered, "the error claims the update is visible at the mapping path")
    test.assertIn("published", lowered, "the error does not say the update was published")
    test.assertIn("could not be confirmed", lowered)


class PathReplacedBeforeReadbackTests(unittest.TestCase):
    def save_with_outside_change(self, directory: str, kind: str) -> tuple[Path, MappingDurabilityError]:
        path, store = seeded_store(directory)
        original_sync = store._fsync_parent_directory

        def sync_then_replace() -> None:
            original_sync()
            replace_path(path, kind)

        with patch.object(store, "_fsync_parent_directory", sync_then_replace):
            with self.assertRaises(MappingDurabilityError) as raised:
                store.save_mapping(mapping(2, "published"))
        return path, raised.exception

    def test_the_error_reports_publication_without_claiming_the_path_shows_it(self) -> None:
        for kind in EXPECTED_OUTSIDE:
            with self.subTest(outside=kind), tempfile.TemporaryDirectory() as directory:
                _path, error = self.save_with_outside_change(directory, kind)
                assert_no_visibility_claim(self, error.public_detail)
                assert_no_visibility_claim(self, str(error))

    def test_the_route_response_reports_publication_without_claiming_the_path_shows_it(self) -> None:
        handler = app_main.app.exception_handlers[MappingDurabilityError]
        with tempfile.TemporaryDirectory() as directory:
            _path, error = self.save_with_outside_change(directory, "other_document")
        response = asyncio.run(handler(Mock(), error))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["error"], "mapping_durability_indeterminate")
        assert_no_visibility_claim(self, body["detail"])

    def test_the_outside_content_is_kept_and_no_temporary_file_is_left(self) -> None:
        for kind, expected in EXPECTED_OUTSIDE.items():
            with self.subTest(outside=kind), tempfile.TemporaryDirectory() as directory:
                path, _error = self.save_with_outside_change(directory, kind)
                self.assertEqual(outside_content(path, kind), expected, "the save rolled back outside content")
                self.assertEqual(list(path.parent.glob(f"{path.name}.*.tmp")), [])

    def test_a_save_with_no_outside_change_succeeds_and_the_path_holds_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            saved = store.save_mapping(mapping(2, "published"))
            self.assertEqual(saved.notes, "published")
            self.assertEqual(MappingStore(path).get_mapping("sys-a", "shelf-a", 2).notes, "published")
            self.assertEqual(MappingStore(path).get_mapping("sys-a", "shelf-a", 1).notes, "seed")


class DescriptorReadbackBoundaryTests(unittest.TestCase):
    """The readback's evidence is the file its descriptor opened, as documented.

    A replacement of the pathname after that descriptor opened the published
    file is not seen by the readback, so the save succeeds; the pathname keeps
    the outside document and is never rolled back.
    """

    def test_a_replacement_after_the_readback_opened_is_kept_and_not_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            original_fstat = os.fstat
            readback = {"active": False, "replaced": False}
            original_validate = store._validate_published_bytes

            def validate(expected: bytes) -> None:
                readback["active"] = True
                try:
                    original_validate(expected)
                finally:
                    readback["active"] = False

            def fstat(descriptor, *args, **kwargs):
                result = original_fstat(descriptor, *args, **kwargs)
                if readback["active"] and not readback["replaced"]:
                    readback["replaced"] = True
                    replace_path(path, "other_document")
                return result

            with (
                patch.object(store, "_validate_published_bytes", validate),
                patch("app.services.mapping_store.os.fstat", fstat),
            ):
                saved = store.save_mapping(mapping(2, "published"))
            self.assertTrue(readback["replaced"], "the readback never opened its descriptor")
            self.assertEqual(saved.notes, "published")
            self.assertEqual(path.read_bytes(), OUTSIDE_DOCUMENT)


if __name__ == "__main__":
    unittest.main()
