from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SEGMENTED_BACKUP_SCHEMA_VERSION = 2
SEGMENT_CATALOG_VERSION = 1
HISTORY_SEGMENT_GROUP_KEY = "history_segments"
MIGRATION_PENDING_MARKER = ".migration-pending.json"
ACTIVATION_PENDING_SUFFIX = ".segmented-activation-pending.json"
MAX_HISTORY_SEGMENT_BYTES = 1536 * 1024 * 1024
SEGMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def path_entry_exists(path: str | os.PathLike[str]) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def activation_pending_path(hot_path: str | os.PathLike[str]) -> Path:
    hot = Path(hot_path).absolute()
    return hot.with_name(f".{hot.name}{ACTIVATION_PENDING_SUFFIX}")


def _require_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SEGMENT_ID_PATTERN.fullmatch(value):
        raise ValueError(f"Backup bundle {label} is invalid.")
    return value


def _require_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Backup bundle {label} is invalid.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Backup bundle {label} is invalid.") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"Backup bundle {label} is invalid.")
    return timestamp


def validate_segmented_manifest(manifest: dict[str, Any]) -> None:
    generation = manifest.get("generation")
    if not isinstance(generation, dict):
        raise ValueError("Backup bundle generation must be an object.")
    _require_identifier(generation.get("generation_id"), label="generation ID")
    if generation.get("complete") is not True:
        raise ValueError("Backup bundle generation is incomplete.")
    if type(generation.get("baseline")) is not bool:
        raise ValueError("Backup bundle generation baseline flag is invalid.")
    parent_generation_id = generation.get("parent_generation_id")
    if parent_generation_id is not None:
        _require_identifier(parent_generation_id, label="parent generation ID")
    if type(generation.get("min_reader_version")) is not int:
        raise ValueError("Backup bundle generation minimum reader version is invalid.")
    if generation["min_reader_version"] > SEGMENTED_BACKUP_SCHEMA_VERSION:
        raise ValueError("Backup bundle requires a newer segmented-history reader.")

    catalog = manifest.get("history_catalog")
    if not isinstance(catalog, dict):
        raise ValueError("Backup bundle history catalog must be an object.")
    if type(catalog.get("catalog_version")) is not int or catalog["catalog_version"] != SEGMENT_CATALOG_VERSION:
        raise ValueError("Backup bundle history catalog version is unsupported.")
    if catalog.get("hot_member_key") != "history_db":
        raise ValueError("Backup bundle history catalog hot member is invalid.")
    for collection_name in ("segments", "tombstones"):
        collection = catalog.get(collection_name)
        if not isinstance(collection, list):
            raise ValueError(
                f"Backup bundle history catalog {collection_name} must be a list."
            )
        if any(not isinstance(entry, dict) for entry in collection):
            raise ValueError(
                f"Backup bundle history catalog {collection_name} entries must be objects."
            )

    segment_ids: set[str] = set()
    supersedes_by_segment: dict[str, set[str]] = {}
    for segment in catalog["segments"]:
        segment_id = _require_identifier(segment.get("segment_id"), label="segment ID")
        if segment_id in segment_ids:
            raise ValueError("Backup bundle history catalog contains a duplicate segment ID.")
        segment_ids.add(segment_id)
        supersedes = segment.get("supersedes")
        if not isinstance(supersedes, list):
            raise ValueError("Backup bundle history catalog segment supersedes list is invalid.")
        normalized_supersedes = {
            _require_identifier(value, label="superseded segment ID")
            for value in supersedes
        }
        if len(normalized_supersedes) != len(supersedes) or segment_id in normalized_supersedes:
            raise ValueError("Backup bundle history catalog segment supersedes list is invalid.")
        supersedes_by_segment[segment_id] = normalized_supersedes

    for segment in catalog["segments"]:
        segment_id = str(segment["segment_id"])
        coverage_start = _require_timestamp(segment.get("coverage_start"), label="segment coverage")
        coverage_end = _require_timestamp(segment.get("coverage_end"), label="segment coverage")
        if coverage_end < coverage_start:
            raise ValueError("Backup bundle history catalog segment coverage is invalid.")
        _require_timestamp(segment.get("sealed_at"), label="segment sealed timestamp")
        expected_member_key = f"history-segment:{segment_id}"
        expected_archive_path = f"history/segments/{segment_id}.sqlite3"
        if segment.get("member_key") != expected_member_key:
            raise ValueError("Backup bundle history catalog segment member key is invalid.")
        if segment.get("archive_path") != expected_archive_path:
            raise ValueError("Backup bundle history catalog segment archive path is invalid.")
        file_members = [
            entry
            for entry in manifest.get("files", [])
            if isinstance(entry, dict) and entry.get("key") == expected_member_key
        ]
        if len(file_members) != 1:
            raise ValueError("Backup bundle history catalog segment is missing its file member.")
        file_member = file_members[0]
        if (
            file_member.get("group_key") != HISTORY_SEGMENT_GROUP_KEY
            or file_member.get("archive_path") != expected_archive_path
        ):
            raise ValueError("Backup bundle history catalog segment file member is invalid.")
        digest = segment.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Backup bundle history catalog segment digest is invalid.")
        if file_member.get("sha256") != digest:
            raise ValueError("Backup bundle history catalog segment file digest does not match.")
        size_bytes = segment.get("size_bytes")
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("Backup bundle history catalog segment size is invalid.")
        if size_bytes > MAX_HISTORY_SEGMENT_BYTES:
            raise ValueError("Backup bundle history catalog segment size exceeds its byte limit.")
        if file_member.get("size_bytes") != size_bytes:
            raise ValueError("Backup bundle history catalog segment file size does not match.")

    referenced_member_keys = {
        f"history-segment:{segment['segment_id']}" for segment in catalog["segments"]
    }
    for file_member in manifest.get("files", []):
        if not isinstance(file_member, dict):
            continue
        if file_member.get("group_key") != HISTORY_SEGMENT_GROUP_KEY:
            continue
        if file_member.get("key") not in referenced_member_keys:
            raise ValueError("Backup bundle contains an unreferenced history segment file member.")

    tombstone_replacements: dict[str, str] = {}
    for tombstone in catalog["tombstones"]:
        segment_id = _require_identifier(tombstone.get("segment_id"), label="tombstone segment ID")
        replacement_id = _require_identifier(
            tombstone.get("superseded_by"),
            label="tombstone replacement segment ID",
        )
        if (
            segment_id in tombstone_replacements
            or segment_id in segment_ids
            or replacement_id not in segment_ids
            or segment_id not in supersedes_by_segment.get(replacement_id, set())
        ):
            raise ValueError("Backup bundle history catalog tombstone is invalid.")
        tombstone_replacements[segment_id] = replacement_id
    declared_supersedes = {
        superseded_id: replacement_id
        for replacement_id, superseded_ids in supersedes_by_segment.items()
        for superseded_id in superseded_ids
    }
    if declared_supersedes != tombstone_replacements:
        raise ValueError("Backup bundle history catalog tombstones do not match supersedes declarations.")

    hot_members = [
        entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and entry.get("key") == "history_db"
    ]
    if (
        len(hot_members) != 1
        or hot_members[0].get("group_key") != "history_db"
        or hot_members[0].get("archive_path") != "history/history.sqlite3"
        or type(hot_members[0].get("size_bytes")) is not int
        or hot_members[0]["size_bytes"] < 0
        or not isinstance(hot_members[0].get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", hot_members[0]["sha256"]) is None
    ):
        raise ValueError("Backup bundle hot history member is invalid.")
    hot_groups = [
        entry
        for entry in manifest.get("groups", [])
        if isinstance(entry, dict) and entry.get("key") == "history_db"
    ]
    if (
        len(hot_groups) != 1
        or hot_groups[0].get("selected") is not True
        or hot_groups[0].get("present") is not True
        or hot_groups[0].get("restore_mode") != "history_db"
    ):
        raise ValueError("Backup bundle hot history group is invalid.")
