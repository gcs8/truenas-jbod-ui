from __future__ import annotations

import io
import json
import unittest
import zipfile
from unittest.mock import patch

from history_service.segment_catalog import MAX_HISTORY_SEGMENT_BYTES
from history_service.system_backup import BUNDLE_FORMAT, SystemBackupService


class SegmentedHistoryManifestTests(unittest.TestCase):
    @staticmethod
    def _segment_entry(segment_id: str = "segment-0001") -> dict[str, object]:
        return {
            "segment_id": segment_id,
            "sequence": 1,
            "member_key": f"history-segment:{segment_id}",
            "archive_path": f"history/segments/{segment_id}.sqlite3",
            "coverage_start": "2025-01-01T00:00:00+00:00",
            "coverage_end": "2025-12-31T23:59:59+00:00",
            "sealed_at": "2026-01-01T00:00:00+00:00",
            "row_counts": {
                "slot_events": 1,
                "metric_samples": 2,
                "metric_rollups": 0,
            },
            "size_bytes": 1024,
            "sha256": "a" * 64,
            "schema_version": 1,
            "key_id": "generation-key-1",
            "required": True,
            "supersedes": [],
        }

    @staticmethod
    def _file_member(segment: dict[str, object]) -> dict[str, object]:
        return {
            "key": segment["member_key"],
            "group_key": "history_segments",
            "archive_path": segment["archive_path"],
            "size_bytes": segment["size_bytes"],
            "sha256": segment["sha256"],
        }

    def test_manifest_v2_requires_generation_and_history_catalog(self) -> None:
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }

        with self.assertRaisesRegex(ValueError, "generation"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_reader_accepts_complete_empty_catalog(self) -> None:
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [
                {
                    "key": "history_db",
                    "archive_root": "history/history.sqlite3",
                    "selected": True,
                    "present": True,
                    "restore_mode": "history_db",
                }
            ],
            "files": [
                {
                    "key": "history_db",
                    "group_key": "history_db",
                    "archive_path": "history/history.sqlite3",
                    "size_bytes": 0,
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                }
            ],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [],
                "tombstones": [],
            },
        }

        SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_requires_hot_history_size_and_digest(self) -> None:
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [
                {
                    "key": "history_db",
                    "archive_root": "history/history.sqlite3",
                    "selected": True,
                    "present": True,
                    "restore_mode": "history_db",
                }
            ],
            "files": [
                {
                    "key": "history_db",
                    "group_key": "history_db",
                    "archive_path": "history/history.sqlite3",
                }
            ],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "hot history member"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_duplicate_segment_ids(self) -> None:
        segment = self._segment_entry()
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment, dict(segment)],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "duplicate segment"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_tombstone_without_matching_active_replacement(self) -> None:
        segment = self._segment_entry()
        segment["supersedes"] = ["segment-old"]
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [self._file_member(segment)],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": False,
                "parent_generation_id": "generation-0000",
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [
                    {
                        "segment_id": "segment-old",
                        "superseded_by": "segment-missing",
                    }
                ],
            },
        }

        with self.assertRaisesRegex(ValueError, "tombstone"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_catalog_segment_without_file_member(self) -> None:
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [self._segment_entry()],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "file member"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_catalog_file_digest_mismatch(self) -> None:
        segment = self._segment_entry()
        file_member = self._file_member(segment)
        file_member["sha256"] = "b" * 64
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [file_member],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "digest"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_catalog_file_size_mismatch(self) -> None:
        segment = self._segment_entry()
        file_member = self._file_member(segment)
        file_member["size_bytes"] = 1025
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [file_member],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "size"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_unreferenced_history_segment_file(self) -> None:
        segment = self._segment_entry()
        orphan = self._file_member(self._segment_entry("segment-orphan"))
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [self._file_member(segment), orphan],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "unreferenced"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_segment_larger_than_segment_cap(self) -> None:
        segment = self._segment_entry()
        segment["size_bytes"] = MAX_HISTORY_SEGMENT_BYTES + 1
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [self._file_member(segment)],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "segment size"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_segment_with_inverted_coverage_range(self) -> None:
        segment = self._segment_entry()
        segment["coverage_end"] = "2024-12-31T23:59:59+00:00"
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [self._file_member(segment)],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "coverage"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_manifest_v2_rejects_segment_without_sealed_timestamp(self) -> None:
        segment = self._segment_entry()
        segment.pop("sealed_at")
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [self._file_member(segment)],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "sealed"):
            SystemBackupService._validate_manifest_before_extraction(manifest)

    def test_archive_reader_rejects_v2_without_hot_history_before_payload_extraction(self) -> None:
        segment = self._segment_entry()
        manifest = {
            "schema_version": 2,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [self._file_member(segment)],
            "generation": {
                "generation_id": "generation-0001",
                "complete": True,
                "baseline": True,
                "parent_generation_id": None,
                "min_reader_version": 2,
            },
            "history_catalog": {
                "catalog_version": 1,
                "hot_member_key": "history_db",
                "segments": [segment],
                "tombstones": [],
            },
        }
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))
            archive.writestr(str(segment["archive_path"]), b"not-a-sqlite-database")

        reader = SystemBackupService.__new__(SystemBackupService)
        with patch.object(
            SystemBackupService,
            "_extract_manifest_zip_members",
            side_effect=AssertionError("v2 payload extraction must not run"),
        ):
            with self.assertRaisesRegex(ValueError, "hot history"):
                reader._read_archive(archive_bytes.getvalue())


if __name__ == "__main__":
    unittest.main()
