from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import select
import shutil
import sqlite3
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - exercised in runtime validation instead.
    zstd = None

from app import __version__
from app.config import (
    EnclosureProfileConfig,
    Settings,
    _deep_merge,
    _derive_runtime_layout_paths,
    _normalize_systems,
    get_settings,
)
from app.models.domain import ManualMapping, SasFabricAlias
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailCacheEntry
from app.services.storage_views import resolve_system_storage_views
from history_service.config import HistorySettings
from history_service.segment_catalog import (
    HISTORY_SEGMENT_GROUP_KEY,
    SEGMENTED_BACKUP_SCHEMA_VERSION,
    activation_pending_path,
    path_entry_exists,
    validate_segmented_manifest,
)
from history_service.migration_lock import history_write_lock
from history_service.segment_reader import SegmentedHistoryReader
from history_service.segment_sealer import SEGMENT_DIRECTORY_MODE, SEGMENT_FILE_MODE
from history_service.segmented_restore import (
    file_matches as restore_file_matches,
    record_file as record_restore_file,
    record_optional_file as record_optional_restore_file,
    record_optional_tree as record_optional_restore_tree,
    record_tree as record_restore_tree,
    remove_recorded_file as remove_recorded_restore_file,
    remove_recorded_tree as remove_recorded_restore_tree,
    tree_matches as restore_tree_matches,
    write_restore_journal,
)
from history_service.store import SQLITE_CONNECT_TIMEOUT_SECONDS, HistoryStore


BUNDLE_SCHEMA_VERSION = 1
SUPPORTED_BUNDLE_SCHEMA_VERSIONS = frozenset({1, 2})
BUNDLE_FORMAT = "truenas-jbod-ui-backup"
DEBUG_BUNDLE_FORMAT = "truenas-jbod-ui-debug-bundle"

CONFIG_FILE_KEY = "config_file"
RUNTIME_OVERRIDES_FILE_KEY = "runtime_overrides_file"
PROFILE_FILE_KEY = "profile_file"
MAPPING_FILE_KEY = "mapping_file"
SAS_FABRIC_ALIAS_FILE_KEY = "sas_fabric_alias_file"
SLOT_DETAIL_FILE_KEY = "slot_detail_file"
HISTORY_DB_KEY = "history_db"
SEGMENTED_CATALOG_STAGING_KEY = "__segmented_history_catalog__"
SSH_KEYS_KEY = "ssh_keys"
TLS_TRUST_KEY = "tls_trust"
KNOWN_HOSTS_KEY = "known_hosts"
DEBUG_STATE_KEY = "debug_state"
DEBUG_README_KEY = "debug_readme"

BACKUP_GROUP_METADATA: dict[str, dict[str, Any]] = {
    CONFIG_FILE_KEY: {
        "label": "Config",
        "archive_root": "config/config.yaml",
        "sensitive": False,
        "bundle_types": ("backup", "debug"),
        "default_backup": True,
        "default_debug": True,
        "restore_mode": "file",
    },
    RUNTIME_OVERRIDES_FILE_KEY: {
        "label": "Runtime Overrides",
        "archive_root": "config/runtime-overrides.yaml",
        "sensitive": False,
        "bundle_types": ("backup", "debug"),
        "default_backup": True,
        "default_debug": True,
        "restore_mode": "file",
    },
    PROFILE_FILE_KEY: {
        "label": "Profiles",
        "archive_root": "config/profiles.yaml",
        "sensitive": False,
        "bundle_types": ("backup", "debug"),
        "default_backup": True,
        "default_debug": True,
        "restore_mode": "file",
    },
    MAPPING_FILE_KEY: {
        "label": "Mappings",
        "archive_root": "data/slot_mappings.json",
        "sensitive": False,
        "bundle_types": ("backup", "debug"),
        "default_backup": True,
        "default_debug": True,
        "restore_mode": "file",
    },
    SAS_FABRIC_ALIAS_FILE_KEY: {
        "label": "SAS Fabric Aliases",
        "archive_root": "data/sas_fabric_aliases.json",
        "sensitive": False,
        "bundle_types": ("backup", "debug"),
        "default_backup": True,
        "default_debug": True,
        "restore_mode": "file",
    },
    SLOT_DETAIL_FILE_KEY: {
        "label": "Slot Cache",
        "archive_root": "data/slot_detail_cache.json",
        "sensitive": False,
        "bundle_types": ("backup", "debug"),
        "default_backup": True,
        "default_debug": True,
        "restore_mode": "file",
    },
    HISTORY_DB_KEY: {
        "label": "History DB",
        "archive_root": "history/history.sqlite3",
        "sensitive": False,
        "bundle_types": ("backup", "debug"),
        "default_backup": True,
        "default_debug": True,
        "restore_mode": "history_db",
    },
    SSH_KEYS_KEY: {
        "label": "SSH Keys",
        "archive_root": "config/ssh",
        "sensitive": True,
        "bundle_types": ("backup", "debug"),
        "default_backup": False,
        "default_debug": False,
        "restore_mode": "directory",
    },
    TLS_TRUST_KEY: {
        "label": "TLS Trust",
        "archive_root": "config/tls",
        "sensitive": True,
        "bundle_types": ("backup", "debug"),
        "default_backup": False,
        "default_debug": False,
        "restore_mode": "directory",
    },
    KNOWN_HOSTS_KEY: {
        "label": "Known Hosts",
        "archive_root": "data/known_hosts",
        "sensitive": True,
        "bundle_types": ("backup", "debug"),
        "default_backup": False,
        "default_debug": False,
        "restore_mode": "file",
    },
    DEBUG_STATE_KEY: {
        "label": "Debug State",
        "archive_root": "debug/state.json",
        "sensitive": False,
        "bundle_types": ("debug",),
        "default_backup": False,
        "default_debug": True,
        "restore_mode": "generated",
    },
    DEBUG_README_KEY: {
        "label": "Debug README",
        "archive_root": "debug/README.txt",
        "sensitive": False,
        "bundle_types": ("debug",),
        "default_backup": False,
        "default_debug": True,
        "restore_mode": "generated",
    },
}

DEFAULT_BACKUP_GROUP_KEYS: tuple[str, ...] = tuple(
    key
    for key, item in BACKUP_GROUP_METADATA.items()
    if "backup" in item["bundle_types"] and item["default_backup"]
)
DEFAULT_DEBUG_GROUP_KEYS: tuple[str, ...] = tuple(
    key
    for key, item in BACKUP_GROUP_METADATA.items()
    if "debug" in item["bundle_types"] and item["default_debug"]
)
SENSITIVE_GROUP_KEYS: set[str] = {
    key
    for key, item in BACKUP_GROUP_METADATA.items()
    if bool(item["sensitive"])
}

ArchivePackaging = Literal["tar.zst", "zip", "tar.gz", "7z"]
SUPPORTED_ARCHIVE_PACKAGING: tuple[ArchivePackaging, ...] = ("tar.zst", "zip", "tar.gz", "7z")
ARCHIVE_FILE_SUFFIXES: dict[ArchivePackaging, str] = {
    "tar.zst": ".tar.zst",
    "zip": ".zip",
    "tar.gz": ".tar.gz",
    "7z": ".7z",
}
ARCHIVE_MEDIA_TYPES: dict[ArchivePackaging, str] = {
    "tar.zst": "application/zstd",
    "zip": "application/zip",
    "tar.gz": "application/gzip",
    "7z": "application/x-7z-compressed",
}
SEVEN_ZIP_SIGNATURE = b"\x37\x7a\xbc\xaf\x27\x1c"
ENCRYPTED_BACKUP_MAGIC = b"TJBENC01"
ENCRYPTED_BACKUP_SALT_BYTES = 16
ENCRYPTED_BACKUP_NONCE_BYTES = 12
ENCRYPTED_BACKUP_TAG_BYTES = 16
ENCRYPTED_BACKUP_SCRYPT_N = 2**15
ENCRYPTED_BACKUP_SCRYPT_R = 8
ENCRYPTED_BACKUP_SCRYPT_P = 1
SEVEN_ZIP_TIMEOUT_SECONDS = 600
SEVEN_ZIP_BINARY = "7z"
MAX_BACKUP_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_FILE_BACKED_BACKUP_ARCHIVE_BYTES = 6 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_COUNT = 1024
MAX_ARCHIVE_MEMBER_BYTES = 1536 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES = 6 * 1024 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_ARCHIVE_METADATA_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_7Z_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
ARCHIVE_READ_CHUNK_BYTES = 1024 * 1024

ExtractedMember = bytes | Path


@dataclass(slots=True)
class BundleMember:
    key: str
    group_key: str
    archive_path: str
    source_path: str | None
    present: bool
    content: bytes | None
    file_path: Path | None = None


@dataclass(slots=True)
class BundleGroup:
    key: str
    label: str
    archive_root: str
    source_path: str | None
    selected: bool
    present: bool
    sensitive: bool
    restore_mode: str


@dataclass(slots=True)
class BackupArtifact:
    filename: str
    content: bytes
    media_type: str
    manifest: dict[str, Any]


@dataclass(slots=True)
class FileBackupArtifact:
    filename: str
    path: Path
    media_type: str
    manifest: dict[str, Any]
    cleanup_root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.cleanup_root, ignore_errors=True)


@dataclass(slots=True)
class _SegmentedExportSnapshot:
    hot_path: Path
    catalog: dict[str, Any]
    segment_paths: tuple[Path, ...]


@dataclass(slots=True)
class _ImportRollbackEntry:
    target_path: Path
    kind: Literal["missing", "file", "directory", "symlink", "history"]
    backup_path: Path | None = None
    history_store: HistoryStore | None = None
    mutated: bool = False


@dataclass(slots=True)
class _PreparedSegmentedRestore:
    store: HistoryStore
    hot_member_key: str
    segment_members: tuple[tuple[str, Path], ...]
    hot_entry: _ImportRollbackEntry
    segments_entry: _ImportRollbackEntry
    staged_hot_path: Path
    staged_segments_path: Path
    previous_hot_path: Path
    previous_segments_path: Path
    journal_payload: dict[str, Any]


class _ImportActivationTransaction:
    _MISSING_FILE_MODE = 0o600
    _MISSING_DIRECTORY_MODE = 0o700

    def __init__(
        self,
        extracted_members: dict[str, ExtractedMember],
        *,
        history_store: HistoryStore | None = None,
    ) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-import-"))
        self.staging_root = self.root / "staged"
        self.rollback_root = self.root / "rollback"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.rollback_root.mkdir(parents=True, exist_ok=True)
        self._staged_members: dict[str, Path] = {}
        self._journal: dict[str, _ImportRollbackEntry] = {}
        self._journal_order: list[str] = []
        self._created_parents: list[Path] = []
        self._sibling_artifacts: set[Path] = set()
        self._protected_history_key = (
            self._journal_key(history_store.file_path)
            if history_store is not None
            else None
        )
        self._committed = False
        self._rollback_completed = False
        self._prepared_segmented_restore: _PreparedSegmentedRestore | None = None
        self._preserve_segmented_evidence = False
        try:
            for member_key, content in sorted(extracted_members.items()):
                staged_path = self.staging_root / hashlib.sha256(
                    member_key.encode("utf-8")
                ).hexdigest()
                if isinstance(content, Path):
                    self._copy_staged_file(staged_path, content)
                else:
                    self._write_staged_bytes(staged_path, content)
                self._staged_members[member_key] = staged_path
            self._fsync_directory(self.staging_root)
            self._fsync_directory(self.rollback_root)
            self._fsync_directory(self.root)
        except Exception:
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    def __enter__(self) -> "_ImportActivationTransaction":
        return self

    def __exit__(self, exc_type, exc, traceback) -> Literal[False]:
        if exc is not None and self._preserve_segmented_evidence:
            raise RuntimeError(
                "Segmented history restore integrity failed; recovery material was "
                f"preserved at {self.root}."
            ) from exc
        if exc is not None and not self._committed:
            try:
                self.rollback()
            except Exception as rollback_error:
                raise RuntimeError(
                    "Import activation rollback is incomplete; recovery material was "
                    f"preserved at {self.root}: {rollback_error}"
                ) from exc
            try:
                self._cleanup_root()
            except Exception as cleanup_error:
                raise RuntimeError(
                    "Import activation rollback cleanup is incomplete; recovery material "
                    f"was preserved at {self.root}: {cleanup_error}"
                ) from exc
            self._rollback_completed = True
            return False

        if self._committed:
            try:
                self._finalize_commit()
                self._cleanup_root()
            except Exception as cleanup_error:
                raise RuntimeError(
                    "Import activation cleanup is incomplete; recovery material was "
                    f"preserved at {self.root}: {cleanup_error}"
                ) from cleanup_error
        else:
            self._cleanup_root()
        return False

    def commit(self) -> None:
        self._committed = True

    @property
    def rollback_completed(self) -> bool:
        return self._rollback_completed

    def activate_file(self, target_path: Path, member_key: str) -> None:
        self._activate_file(target_path, member_key, allow_history=False)

    def _activate_file(
        self,
        target_path: Path,
        member_key: str,
        *,
        allow_history: bool,
    ) -> None:
        entry = self._record_target(target_path, allow_history=allow_history)
        staged_path = self._staged_member(member_key)
        self._ensure_parent_hierarchy(target_path.parent)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.restore-",
            dir=target_path.parent,
        )
        temp_path = Path(temp_name)
        self._sibling_artifacts.add(temp_path)
        try:
            os.close(file_descriptor)
            shutil.copyfile(staged_path, temp_path)
            file_owner = self._existing_owner(
                target_path if entry.kind == "file" else target_path.parent,
                directory=entry.kind != "file",
            )
            file_mode = (
                target_path.stat(follow_symlinks=False).st_mode & 0o7777
                if entry.kind == "file"
                else self._MISSING_FILE_MODE
            )
            self._apply_owner(temp_path, file_owner)
            temp_path.chmod(file_mode)
            self._fsync_file(temp_path)
            self._park_original(entry)
            os.replace(temp_path, target_path)
            self._sibling_artifacts.discard(temp_path)
            entry.mutated = True
            self._fsync_directory(target_path.parent)
        finally:
            self._cleanup_sibling_artifact(temp_path)

    def prepare_segmented_history(
        self,
        store: HistoryStore,
        *,
        hot_member_key: str,
        segment_members: list[tuple[str, Path]],
        generation_id: str | None,
    ) -> dict[str, Any]:
        if self._prepared_segmented_restore is not None:
            raise ValueError("Segmented history restore is already prepared.")
        catalog_path = store.segment_catalog_path
        if catalog_path is None:
            raise ValueError("Segmented history catalog target is not configured.")
        store._segment_reader_cache = None
        store._segment_reader_identity = None
        self._checkpoint_hot_database(store.file_path)
        hot_entry = self._record_target(store.file_path, allow_history=True)
        segments_entry = self._record_target(catalog_path.parent)
        if hot_entry.kind not in {"file", "missing"}:
            raise ValueError("Live segmented history hot target is invalid.")
        if segments_entry.kind not in {"directory", "missing"}:
            raise ValueError("Live segmented history directory target is invalid.")
        self._ensure_parent_hierarchy(store.file_path.parent)
        self._ensure_parent_hierarchy(catalog_path.parent.parent)

        transaction_id = uuid.uuid4().hex
        staged_hot_path = store.file_path.with_name(
            f".{store.file_path.name}.restore-{transaction_id}"
        )
        previous_hot_path = store.file_path.with_name(
            f".{store.file_path.name}.previous-{transaction_id}"
        )
        staged_segments_path = catalog_path.parent.with_name(
            f".{catalog_path.parent.name}.restore-{transaction_id}"
        )
        previous_segments_path = catalog_path.parent.with_name(
            f".{catalog_path.parent.name}.previous-{transaction_id}"
        )
        for path in (
            staged_hot_path,
            previous_hot_path,
            staged_segments_path,
            previous_segments_path,
        ):
            if self._path_exists(path):
                raise ValueError("Segmented history restore artifact already exists.")

        self._stage_segmented_hot(
            staged_hot_path,
            source_path=self._staged_member(hot_member_key),
            target_path=store.file_path,
            entry=hot_entry,
        )
        self._stage_segmented_directory(
            staged_segments_path,
            target_dir=catalog_path.parent,
            entry=segments_entry,
            members=segment_members,
        )
        journal_payload: dict[str, Any] = {
            "journal_version": 1,
            "operation": "segmented-restore",
            "transaction_id": transaction_id,
            "phase": "prepared",
            "generation_id": generation_id,
            "hot": {
                "target_name": store.file_path.name,
                "staged_name": staged_hot_path.name,
                "previous_name": previous_hot_path.name,
                "prior": record_optional_restore_file(store.file_path),
                "candidate": record_restore_file(staged_hot_path),
            },
            "segments": {
                "target_name": catalog_path.parent.name,
                "staged_name": staged_segments_path.name,
                "previous_name": previous_segments_path.name,
                "prior": record_optional_restore_tree(catalog_path.parent),
                "candidate": record_restore_tree(staged_segments_path),
            },
        }
        self._prepared_segmented_restore = _PreparedSegmentedRestore(
            store=store,
            hot_member_key=hot_member_key,
            segment_members=tuple(segment_members),
            hot_entry=hot_entry,
            segments_entry=segments_entry,
            staged_hot_path=staged_hot_path,
            staged_segments_path=staged_segments_path,
            previous_hot_path=previous_hot_path,
            previous_segments_path=previous_segments_path,
            journal_payload=journal_payload,
        )
        return journal_payload

    def _stage_segmented_hot(
        self,
        staged_path: Path,
        *,
        source_path: Path,
        target_path: Path,
        entry: _ImportRollbackEntry,
    ) -> None:
        descriptor = os.open(
            staged_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            self._MISSING_FILE_MODE,
        )
        os.close(descriptor)
        self._sibling_artifacts.add(staged_path)
        shutil.copyfile(source_path, staged_path)
        owner = self._existing_owner(
            target_path if entry.kind == "file" else target_path.parent,
            directory=entry.kind != "file",
        )
        mode = (
            target_path.stat(follow_symlinks=False).st_mode & 0o7777
            if entry.kind == "file"
            else self._MISSING_FILE_MODE
        )
        self._apply_owner(staged_path, owner)
        staged_path.chmod(mode)
        self._fsync_file(staged_path)
        self._fsync_directory(staged_path.parent)

    def _stage_segmented_directory(
        self,
        staged_dir: Path,
        *,
        target_dir: Path,
        entry: _ImportRollbackEntry,
        members: list[tuple[str, Path]],
    ) -> None:
        staged_dir.mkdir(mode=SEGMENT_DIRECTORY_MODE)
        self._sibling_artifacts.add(staged_dir)
        existing_directory = target_dir if entry.kind == "directory" else None
        root_owner = self._existing_owner(
            existing_directory or target_dir.parent,
            directory=True,
        )
        self._apply_owner(staged_dir, root_owner)
        staged_dir.chmod(
            self._existing_mode(existing_directory, directory=True)
            or SEGMENT_DIRECTORY_MODE
        )
        seen: set[Path] = set()
        for member_key, relative_path in members:
            if (
                relative_path.is_absolute()
                or not relative_path.parts
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or relative_path in seen
            ):
                raise ValueError("Segmented history restore member path is invalid.")
            seen.add(relative_path)
            staged_target = staged_dir / relative_path
            current_relative = Path()
            for part in relative_path.parts[:-1]:
                current_relative /= part
                staged_parent = staged_dir / current_relative
                if not staged_parent.exists():
                    staged_parent.mkdir()
                existing_parent = (
                    existing_directory / current_relative
                    if existing_directory is not None
                    else None
                )
                parent_owner = self._existing_owner(existing_parent, directory=True)
                if parent_owner is None:
                    parent_owner = self._existing_owner(
                        staged_parent.parent,
                        directory=True,
                    )
                self._apply_owner(staged_parent, parent_owner)
                staged_parent.chmod(
                    self._existing_mode(existing_parent, directory=True)
                    or SEGMENT_DIRECTORY_MODE
                )
            shutil.copyfile(self._staged_member(member_key), staged_target)
            existing_target = (
                existing_directory / relative_path
                if existing_directory is not None
                else None
            )
            target_owner = self._existing_owner(existing_target, directory=False)
            if target_owner is None:
                target_owner = self._existing_owner(
                    staged_target.parent,
                    directory=True,
                )
            self._apply_owner(staged_target, target_owner)
            staged_target.chmod(
                self._existing_mode(existing_target, directory=False)
                or SEGMENT_FILE_MODE
            )
        self._fsync_tree(staged_dir)
        self._fsync_directory(staged_dir.parent)

    def _activate_prepared_target(
        self,
        entry: _ImportRollbackEntry,
        *,
        staged_path: Path,
        previous_path: Path,
        prior_record: dict[str, Any],
        candidate_record: dict[str, Any],
        matcher: Callable[[Path, Any], bool],
    ) -> None:
        prior_matches = (
            not self._path_exists(entry.target_path)
            if prior_record.get("kind") == "missing"
            else matcher(entry.target_path, prior_record)
        )
        if not prior_matches or not matcher(staged_path, candidate_record):
            self._preserve_segmented_evidence = True
            raise ValueError("Segmented history restore prepared artifact integrity failed.")
        if entry.kind != "missing":
            if self._path_exists(previous_path):
                raise ValueError("Segmented history prior restore artifact already exists.")
            os.replace(entry.target_path, previous_path)
            entry.backup_path = previous_path
            entry.mutated = True
            self._sibling_artifacts.add(previous_path)
            self._fsync_directory(entry.target_path.parent)
            if not matcher(previous_path, prior_record):
                self._preserve_segmented_evidence = True
                raise ValueError("Segmented history restore parked prior integrity failed.")
        os.replace(staged_path, entry.target_path)
        self._sibling_artifacts.discard(staged_path)
        entry.mutated = True
        self._fsync_directory(entry.target_path.parent)
        if not matcher(entry.target_path, candidate_record):
            self._preserve_segmented_evidence = True
            raise ValueError("Segmented history restore activated candidate integrity failed.")

    def activate_segmented_history(
        self,
        store: HistoryStore,
        *,
        hot_member_key: str,
        segment_members: list[tuple[str, Path]],
    ) -> None:
        prepared = self._prepared_segmented_restore
        if (
            prepared is None
            or prepared.store is not store
            or prepared.hot_member_key != hot_member_key
            or prepared.segment_members != tuple(segment_members)
        ):
            raise ValueError("Segmented history restore was not prepared.")
        for suffix in ("-wal", "-shm", "-journal"):
            if path_entry_exists(Path(f"{store.file_path}{suffix}")):
                self._preserve_segmented_evidence = True
                raise ValueError(
                    "Segmented history restore refuses post-marker SQLite sidecars."
                )
        self._activate_prepared_target(
            prepared.hot_entry,
            staged_path=prepared.staged_hot_path,
            previous_path=prepared.previous_hot_path,
            prior_record=prepared.journal_payload["hot"]["prior"],
            candidate_record=prepared.journal_payload["hot"]["candidate"],
            matcher=restore_file_matches,
        )
        self._activate_prepared_target(
            prepared.segments_entry,
            staged_path=prepared.staged_segments_path,
            previous_path=prepared.previous_segments_path,
            prior_record=prepared.journal_payload["segments"]["prior"],
            candidate_record=prepared.journal_payload["segments"]["candidate"],
            matcher=restore_tree_matches,
        )

    @staticmethod
    def _checkpoint_hot_database(hot_path: Path) -> None:
        if path_entry_exists(Path(f"{hot_path}-journal")):
            raise ValueError(
                "History database has a rollback journal; recover it before a segmented restore."
            )
        if not path_entry_exists(hot_path):
            if any(
                path_entry_exists(Path(f"{hot_path}{suffix}"))
                for suffix in ("-wal", "-shm")
            ):
                raise ValueError(
                    "History database sidecars exist without the main database; recover them "
                    "before a segmented restore."
                )
            return
        wal_path = Path(f"{hot_path}-wal")
        shm_path = Path(f"{hot_path}-shm")
        if not path_entry_exists(wal_path) and not path_entry_exists(shm_path):
            return
        with closing(sqlite3.connect(hot_path, timeout=SQLITE_CONNECT_TIMEOUT_SECONDS)) as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy, pending_frames, _checkpointed = (int(value) for value in row)
        if busy or pending_frames > 0:
            raise ValueError(
                "History database WAL cannot be checkpointed while readers hold it open "
                f"({max(pending_frames, 0)} frames pending); stop the history service and retry "
                "the segmented restore."
            )
        if path_entry_exists(wal_path) or path_entry_exists(shm_path):
            raise ValueError(
                "History database sidecars remain after checkpoint; stop the history service, "
                "remove only checkpointed residual sidecars, and retry the segmented restore."
            )

    def activate_directory(
        self,
        target_dir: Path,
        members: list[tuple[str, Path]],
    ) -> None:
        entry = self._record_target(target_dir)
        existing_directory = target_dir if entry.kind == "directory" else None
        self._ensure_parent_hierarchy(target_dir.parent)
        staged_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{target_dir.name}.restore-",
                dir=target_dir.parent,
            )
        )
        self._sibling_artifacts.add(staged_dir)
        root_owner = self._existing_owner(
            existing_directory or target_dir.parent,
            directory=True,
        )
        root_mode = self._existing_mode(existing_directory, directory=True)
        self._apply_owner(staged_dir, root_owner)
        staged_dir.chmod(root_mode or self._MISSING_DIRECTORY_MODE)
        try:
            for member_key, relative_path in members:
                staged_target = staged_dir / relative_path
                current_relative = Path()
                for part in relative_path.parts[:-1]:
                    current_relative /= part
                    staged_parent = staged_dir / current_relative
                    if not staged_parent.exists():
                        staged_parent.mkdir()
                    existing_parent = (
                        existing_directory / current_relative
                        if existing_directory is not None
                        else None
                    )
                    parent_owner = self._existing_owner(existing_parent, directory=True)
                    if parent_owner is None:
                        parent_owner = self._existing_owner(
                            staged_parent.parent,
                            directory=True,
                        )
                    self._apply_owner(staged_parent, parent_owner)
                    staged_parent.chmod(
                        self._existing_mode(existing_parent, directory=True)
                        or self._MISSING_DIRECTORY_MODE
                    )
                shutil.copyfile(self._staged_member(member_key), staged_target)
                existing_target = (
                    existing_directory / relative_path
                    if existing_directory is not None
                    else None
                )
                target_owner = self._existing_owner(existing_target, directory=False)
                if target_owner is None:
                    target_owner = self._existing_owner(
                        staged_target.parent,
                        directory=True,
                    )
                self._apply_owner(staged_target, target_owner)
                staged_target.chmod(
                    self._existing_mode(existing_target, directory=False)
                    or self._MISSING_FILE_MODE
                )
            self._fsync_tree(staged_dir)
            self._park_original(entry)
            os.replace(staged_dir, target_dir)
            self._sibling_artifacts.discard(staged_dir)
            entry.mutated = True
            self._fsync_directory(target_dir.parent)
        finally:
            self._cleanup_sibling_artifact(staged_dir)

    def activate_history(self, store: HistoryStore, member_key: str) -> None:
        entry = self._record_history(store)
        self._ensure_parent_hierarchy(entry.target_path.parent)
        entry.mutated = True
        store.restore_backup(self._staged_member(member_key))
        self._fsync_file(entry.target_path)
        self._fsync_directory(entry.target_path.parent)

    def rollback(self) -> None:
        failures: list[tuple[str, Exception]] = []
        for journal_key in reversed(self._journal_order):
            entry = self._journal[journal_key]
            try:
                if entry.kind == "history":
                    self._rollback_history(entry)
                else:
                    self._restore_target(entry)
            except Exception as rollback_error:
                failures.append((str(entry.target_path), rollback_error))
        if failures:
            raise RuntimeError(self._failure_summary(failures))

        cleanup_failures = self._cleanup_sibling_artifacts()
        cleanup_failures.extend(self._remove_created_parent_hierarchy())
        if cleanup_failures:
            raise RuntimeError(self._failure_summary(cleanup_failures))

    def _staged_member(self, member_key: str) -> Path:
        staged_path = self._staged_members.get(member_key)
        if staged_path is None:
            raise ValueError(f"Backup bundle is missing staged member {member_key!r}.")
        return staged_path

    def _record_target(
        self,
        target_path: Path,
        *,
        allow_history: bool = False,
    ) -> _ImportRollbackEntry:
        journal_key = self._journal_key(target_path)
        if not allow_history and self._protected_history_key and self._journal_paths_overlap(
            journal_key,
            self._protected_history_key,
        ):
            raise ValueError(
                f"Live restore target {target_path} collides with the history database."
            )
        for existing_key in self._journal:
            if self._journal_paths_overlap(journal_key, existing_key):
                raise ValueError(
                    f"Live restore target {target_path} collides with another restore target."
                )
        if target_path.is_symlink():
            entry = _ImportRollbackEntry(target_path=target_path, kind="symlink")
        elif target_path.is_dir():
            entry = _ImportRollbackEntry(target_path=target_path, kind="directory")
        elif target_path.is_file():
            entry = _ImportRollbackEntry(target_path=target_path, kind="file")
        elif target_path.exists():
            raise ValueError(f"Live restore target {target_path} is not a regular file or directory.")
        else:
            entry = _ImportRollbackEntry(target_path=target_path, kind="missing")
        self._journal[journal_key] = entry
        self._journal_order.append(journal_key)
        return entry

    def _record_history(self, store: HistoryStore) -> _ImportRollbackEntry:
        target_path = store.file_path
        self._reject_symlinked_history_target(target_path)
        journal_key = self._journal_key(target_path)
        for existing_key in self._journal:
            if self._journal_paths_overlap(journal_key, existing_key):
                raise ValueError(
                    f"Live restore target {target_path} collides with the history database."
                )
        if target_path.exists() and not target_path.is_file():
            raise ValueError(f"Live history restore target {target_path} is not a regular file.")
        if target_path.exists():
            backup_dir = self.rollback_root / f"history-{len(self._journal_order):04d}"
            backup_path = store.create_backup(
                backup_dir,
                retention_count=1,
                long_term_backup_dir=None,
                weekly_retention_count=0,
                monthly_retention_count=0,
            )
            if backup_path is None:
                raise ValueError("Unable to create rollback snapshot for the live history database.")
            backup_path = Path(backup_path)
            self._fsync_file(backup_path)
            self._fsync_directory(backup_path.parent)
            self._fsync_directory(self.rollback_root)
            entry = _ImportRollbackEntry(
                target_path=target_path,
                kind="history",
                backup_path=backup_path,
                history_store=store,
            )
        else:
            entry = _ImportRollbackEntry(
                target_path=target_path,
                kind="history",
                history_store=store,
            )
        self._journal[journal_key] = entry
        self._journal_order.append(journal_key)
        return entry

    def _restore_target(self, entry: _ImportRollbackEntry) -> None:
        if not entry.mutated:
            return
        displaced_path: Path | None = None
        if self._path_exists(entry.target_path):
            displaced_path = self._new_sibling_path(entry.target_path, "restore")
            os.replace(entry.target_path, displaced_path)
            self._sibling_artifacts.add(displaced_path)
            self._fsync_directory(entry.target_path.parent)

        try:
            if entry.kind != "missing":
                if entry.backup_path is None or not self._path_exists(entry.backup_path):
                    raise RuntimeError(f"Rollback material is missing for {entry.target_path}.")
                os.replace(entry.backup_path, entry.target_path)
                self._sibling_artifacts.discard(entry.backup_path)
                entry.backup_path = None
                self._fsync_directory(entry.target_path.parent)
        except Exception as restore_error:
            recovery_error: Exception | None = None
            if (
                displaced_path is not None
                and self._path_exists(displaced_path)
                and not self._path_exists(entry.target_path)
            ):
                try:
                    os.replace(displaced_path, entry.target_path)
                    self._sibling_artifacts.discard(displaced_path)
                    self._fsync_directory(entry.target_path.parent)
                except Exception as exc:
                    recovery_error = exc
            if recovery_error is not None:
                raise RuntimeError(
                    f"{restore_error}; active-target recovery also failed: {recovery_error}"
                ) from restore_error
            raise

        if displaced_path is not None:
            self._cleanup_sibling_artifact(displaced_path)
        entry.mutated = False

    def _rollback_history(self, entry: _ImportRollbackEntry) -> None:
        if not entry.mutated:
            return
        if entry.history_store is None:
            raise RuntimeError("Rollback history store is missing.")
        if entry.backup_path is None:
            failures: list[tuple[str, Exception]] = []
            for suffix in ("", "-shm", "-wal"):
                sidecar_path = Path(f"{entry.target_path}{suffix}")
                try:
                    sidecar_path.unlink(missing_ok=True)
                except Exception as unlink_error:
                    failures.append((str(sidecar_path), unlink_error))
            try:
                self._fsync_directory(entry.target_path.parent)
            except Exception as fsync_error:
                failures.append((str(entry.target_path.parent), fsync_error))
            if failures:
                raise RuntimeError(self._failure_summary(failures))
        else:
            entry.history_store.restore_backup(entry.backup_path)
            self._fsync_file(entry.target_path)
            self._fsync_directory(entry.target_path.parent)
        entry.mutated = False

    def _park_original(self, entry: _ImportRollbackEntry) -> None:
        if entry.kind == "missing":
            return
        previous_path = self._new_sibling_path(entry.target_path, "previous")
        os.replace(entry.target_path, previous_path)
        entry.backup_path = previous_path
        entry.mutated = True
        self._sibling_artifacts.add(previous_path)
        self._fsync_directory(entry.target_path.parent)

    def _ensure_parent_hierarchy(self, parent_path: Path) -> None:
        self._reject_symlinked_hierarchy(parent_path)
        missing: list[Path] = []
        cursor = parent_path
        while not self._path_exists(cursor):
            missing.append(cursor)
            if cursor == cursor.parent:
                break
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir()
            self._created_parents.append(directory)
            self._apply_owner(
                directory,
                self._existing_owner(directory.parent, directory=True),
            )
            directory.chmod(self._MISSING_DIRECTORY_MODE)
            self._fsync_directory(directory)
            self._fsync_directory(directory.parent)

    @staticmethod
    def _reject_symlinked_hierarchy(path: Path) -> None:
        cursor = path
        while True:
            if cursor.is_symlink():
                raise ValueError("Import target parent hierarchy must not contain symlinks.")
            if cursor == cursor.parent:
                return
            cursor = cursor.parent

    def _remove_created_parent_hierarchy(self) -> list[tuple[str, Exception]]:
        failures: list[tuple[str, Exception]] = []
        for directory in reversed(self._created_parents):
            try:
                if directory.exists():
                    directory.rmdir()
                    self._fsync_directory(directory.parent)
            except Exception as cleanup_error:
                failures.append((str(directory), cleanup_error))
        if not failures:
            self._created_parents.clear()
        return failures

    def _finalize_commit(self) -> None:
        prepared = self._prepared_segmented_restore
        if prepared is not None:
            hot = prepared.journal_payload["hot"]
            segments = prepared.journal_payload["segments"]
            if not restore_file_matches(prepared.hot_entry.target_path, hot["candidate"]):
                raise ValueError("Segmented history restore live hot integrity failed.")
            if not restore_tree_matches(prepared.segments_entry.target_path, segments["candidate"]):
                raise ValueError("Segmented history restore live segment tree integrity failed.")
            if hot["prior"].get("kind") == "missing":
                if self._path_exists(prepared.previous_hot_path):
                    raise ValueError("Segmented history restore prior hot artifact is unexpected.")
            elif not restore_file_matches(prepared.previous_hot_path, hot["prior"]):
                raise ValueError("Segmented history restore prior hot integrity failed.")
            if segments["prior"].get("kind") == "missing":
                if self._path_exists(prepared.previous_segments_path):
                    raise ValueError("Segmented history restore prior segment artifact is unexpected.")
            elif not restore_tree_matches(
                prepared.previous_segments_path,
                segments["prior"],
            ):
                raise ValueError("Segmented history restore prior segment tree integrity failed.")
            if hot["prior"].get("kind") != "missing":
                remove_recorded_restore_file(
                    prepared.previous_hot_path,
                    hot["prior"],
                    label="prior hot database",
                )
            self._sibling_artifacts.discard(prepared.previous_hot_path)
            if segments["prior"].get("kind") != "missing":
                remove_recorded_restore_tree(
                    prepared.previous_segments_path,
                    segments["prior"],
                    label="prior segment tree",
                )
            self._sibling_artifacts.discard(prepared.previous_segments_path)
            self._prepared_segmented_restore = None
        failures = self._cleanup_sibling_artifacts()
        if failures:
            raise RuntimeError(self._failure_summary(failures))

    def _cleanup_sibling_artifacts(self) -> list[tuple[str, Exception]]:
        failures: list[tuple[str, Exception]] = []
        for artifact_path in sorted(self._sibling_artifacts, key=str):
            try:
                self._cleanup_sibling_artifact(artifact_path)
            except Exception as cleanup_error:
                failures.append((str(artifact_path), cleanup_error))
        return failures

    def _cleanup_sibling_artifact(self, artifact_path: Path) -> None:
        if self._path_exists(artifact_path):
            self._remove_path(artifact_path)
            self._fsync_directory(artifact_path.parent)
        self._sibling_artifacts.discard(artifact_path)

    def _cleanup_root(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def _fsync_tree(self, root: Path) -> None:
        paths = list(root.rglob("*"))
        for path in paths:
            if path.is_file() and not path.is_symlink():
                self._fsync_file(path)
        directories = [path for path in paths if path.is_dir() and not path.is_symlink()]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            self._fsync_directory(directory)
        self._fsync_directory(root)

    @staticmethod
    def _existing_mode(path: Path | None, *, directory: bool) -> int | None:
        if path is None or path.is_symlink():
            return None
        if directory and not path.is_dir():
            return None
        if not directory and not path.is_file():
            return None
        return path.stat(follow_symlinks=False).st_mode & 0o7777

    @staticmethod
    def _existing_owner(path: Path | None, *, directory: bool) -> tuple[int, int] | None:
        if path is None or path.is_symlink():
            return None
        if directory and not path.is_dir():
            return None
        if not directory and not path.is_file():
            return None
        metadata = path.stat(follow_symlinks=False)
        return int(metadata.st_uid), int(metadata.st_gid)

    @staticmethod
    def _apply_owner(path: Path, owner: tuple[int, int] | None) -> None:
        if owner is not None:
            os.chown(path, owner[0], owner[1], follow_symlinks=False)

    @staticmethod
    def _journal_key(target_path: Path) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(target_path)))

    @staticmethod
    def _journal_paths_overlap(first_path: str, second_path: str) -> bool:
        try:
            common_path = os.path.commonpath((first_path, second_path))
        except ValueError:
            return False
        return common_path == first_path or common_path == second_path

    @staticmethod
    def _reject_symlinked_history_target(target_path: Path) -> None:
        cursor = target_path
        while True:
            if cursor.is_symlink():
                raise ValueError(
                    f"Live history restore target {target_path} uses unsafe symlink component {cursor}."
                )
            if cursor == cursor.parent:
                return
            cursor = cursor.parent

    @staticmethod
    def _new_sibling_path(target_path: Path, artifact_kind: str) -> Path:
        while True:
            candidate = target_path.parent / (
                f".{target_path.name}.{artifact_kind}-{uuid.uuid4().hex}"
            )
            if not candidate.exists() and not candidate.is_symlink():
                return candidate

    @staticmethod
    def _failure_summary(failures: list[tuple[str, Exception]]) -> str:
        return "; ".join(
            f"{path}: {type(error).__name__}: {error}"
            for path, error in failures
        )

    @staticmethod
    def _write_staged_bytes(target_path: Path, content: bytes) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def _copy_staged_file(cls, target_path: Path, source_path: Path) -> None:
        source_metadata = source_path.stat(follow_symlinks=False)
        if source_path.is_symlink() or not stat.S_ISREG(source_metadata.st_mode):
            raise ValueError("Backup bundle extracted member is not a regular file.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path, follow_symlinks=False)
        target_path.chmod(cls._MISSING_FILE_MODE)
        cls._fsync_file(target_path)

    @staticmethod
    def _path_exists(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    @classmethod
    def _remove_path(cls, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def default_backup_included_paths() -> list[str]:
    return list(DEFAULT_BACKUP_GROUP_KEYS)


def default_debug_included_paths() -> list[str]:
    return list(DEFAULT_DEBUG_GROUP_KEYS)


def describe_bundle_groups(
    app_settings: Settings,
    history_settings: HistorySettings,
) -> list[dict[str, Any]]:
    layout_paths = _derive_runtime_layout_paths(app_settings.config_file)
    config_root = Path(app_settings.config_file).parent
    source_paths = {
        CONFIG_FILE_KEY: app_settings.config_file,
        RUNTIME_OVERRIDES_FILE_KEY: app_settings.paths.runtime_overrides_file,
        PROFILE_FILE_KEY: app_settings.paths.profile_file,
        MAPPING_FILE_KEY: app_settings.paths.mapping_file,
        SAS_FABRIC_ALIAS_FILE_KEY: app_settings.paths.sas_fabric_alias_file,
        SLOT_DETAIL_FILE_KEY: app_settings.paths.slot_detail_cache_file,
        HISTORY_DB_KEY: history_settings.sqlite_path,
        SSH_KEYS_KEY: str(config_root / "ssh"),
        TLS_TRUST_KEY: str(config_root / "tls"),
        KNOWN_HOSTS_KEY: layout_paths["known_hosts_path"],
        DEBUG_STATE_KEY: "Generated in a temporary export workspace from the current saved stack state.",
        DEBUG_README_KEY: "Generated in a temporary export workspace with support notes for the debug bundle.",
    }
    descriptions: list[dict[str, Any]] = []
    for key, metadata in BACKUP_GROUP_METADATA.items():
        descriptions.append(
            {
                "key": key,
                "label": metadata["label"],
                "path": source_paths.get(key),
                "archive_root": metadata["archive_root"],
                "sensitive": bool(metadata["sensitive"]),
                "bundle_types": list(metadata["bundle_types"]),
                "default_backup": bool(metadata["default_backup"]),
                "default_debug": bool(metadata["default_debug"]),
                "restore_mode": metadata["restore_mode"],
            }
        )
    return descriptions


class DebugScrubber:
    SECRET_FIELD_NAMES = {
        "api_key",
        "api_password",
        "password",
        "sudo_password",
        "passphrase",
        "bootstrap_password",
        "bootstrap_sudo_password",
        "public_key",
    }
    HOST_FIELD_NAMES = {"host", "truenas_host", "ssh_host", "tls_server_name", "connect_host", "server_hostname"}
    USER_FIELD_NAMES = {"api_user", "ssh_user", "bootstrap_user", "service_user"}
    PATH_FIELD_NAMES = {
        "key_path",
        "known_hosts_path",
        "tls_ca_bundle_path",
        "config_file",
        "runtime_overrides_file",
        "profile_file",
        "mapping_file",
        "sas_fabric_alias_file",
        "slot_detail_cache_file",
        "log_file",
        "history_db",
        "bundle_path",
        "source_path",
        "private_path",
        "public_path",
        "runtime_private_path",
        "runtime_public_path",
    }
    DEVICE_NAME_FIELD_NAMES = {"device_name", "multipath_device"}
    DEVICE_IDENTIFIER_FIELD_NAMES = {
        "serial",
        "gptid",
        "disk_identity_key",
        "logical_unit_id",
        "sas_address",
        "attached_sas_address",
        "transport_address",
        "persistent_id",
        "candidate_id",
        "multipath_lunid",
    }
    HOST_LIST_FIELD_NAMES = {"extra_hosts", "ssh_extra_hosts"}
    DEVICE_NAME_LIST_FIELD_NAMES = {"smart_device_names"}
    IDENTIFIER_LIST_FIELD_NAMES = {"identifiers"}

    def __init__(
        self,
        *,
        scrub_secrets: bool = True,
        scrub_disk_identifiers: bool = True,
    ) -> None:
        self.scrub_secrets = scrub_secrets
        self.scrub_disk_identifiers = scrub_disk_identifiers
        self._host_aliases: dict[str, str] = {}
        self._path_aliases: dict[str, str] = {}
        self._user_aliases: dict[str, str] = {}
        self._device_name_aliases: dict[str, str] = {}

    def scrub_payload(self, value: Any, *, parent_key: str | None = None) -> Any:
        if isinstance(value, dict):
            scrubbed: dict[str, Any] = {}
            for key, child in value.items():
                key_text = str(key)
                normalized_key = key_text.lower()
                if self.scrub_secrets and normalized_key in self.SECRET_FIELD_NAMES:
                    scrubbed[key_text] = self._secret_placeholder(normalized_key, child)
                elif self.scrub_secrets and normalized_key in self.HOST_FIELD_NAMES:
                    scrubbed[key_text] = self.alias_host(child)
                elif self.scrub_secrets and normalized_key in self.USER_FIELD_NAMES:
                    scrubbed[key_text] = self.alias_user(child)
                elif self.scrub_secrets and normalized_key in self.PATH_FIELD_NAMES:
                    scrubbed[key_text] = self.alias_path(child)
                elif self.scrub_disk_identifiers and normalized_key in self.DEVICE_NAME_FIELD_NAMES:
                    scrubbed[key_text] = self.alias_device_name(child)
                elif self.scrub_disk_identifiers and normalized_key in self.DEVICE_IDENTIFIER_FIELD_NAMES:
                    scrubbed[key_text] = self.alias_identifier(normalized_key, child)
                elif normalized_key == "details_json" and isinstance(child, str):
                    scrubbed[key_text] = self.scrub_json_text(child)
                else:
                    scrubbed[key_text] = self.scrub_payload(child, parent_key=normalized_key)
            return scrubbed
        if isinstance(value, list):
            normalized_parent = str(parent_key or "").lower()
            if self.scrub_secrets and normalized_parent in self.HOST_LIST_FIELD_NAMES:
                return [self.alias_host(item) for item in value]
            if self.scrub_disk_identifiers and normalized_parent in self.DEVICE_NAME_LIST_FIELD_NAMES:
                return [self.alias_device_name(item) for item in value]
            if self.scrub_disk_identifiers and normalized_parent in self.IDENTIFIER_LIST_FIELD_NAMES:
                return [self.alias_identifier("identifier", item) for item in value]
            return [self.scrub_payload(item, parent_key=parent_key) for item in value]
        return value

    def scrub_json_text(self, raw_text: str) -> str:
        if not self.scrub_secrets and not self.scrub_disk_identifiers:
            return raw_text
        try:
            decoded = json.loads(raw_text)
        except json.JSONDecodeError:
            if self.scrub_secrets:
                return self._secret_placeholder("details_json", raw_text)
            return raw_text
        return json.dumps(self.scrub_payload(decoded), sort_keys=True)

    def alias_host(self, value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return value
        if text in self._host_aliases:
            return self._host_aliases[text]
        alias_host = f"redacted-host-{len(self._host_aliases) + 1:02d}.invalid"
        if "://" in text:
            parsed = urlsplit(text)
            alias_netloc = alias_host
            if parsed.port:
                alias_netloc = f"{alias_netloc}:{parsed.port}"
            alias_value = urlunsplit((parsed.scheme or "https", alias_netloc, parsed.path, parsed.query, parsed.fragment))
        else:
            alias_value = alias_host
        self._host_aliases[text] = alias_value
        return alias_value

    def alias_user(self, value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return value
        if text not in self._user_aliases:
            self._user_aliases[text] = f"user-{len(self._user_aliases) + 1:02d}"
        return self._user_aliases[text]

    def alias_path(self, value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return value
        if text not in self._path_aliases:
            suffix = Path(text).suffix
            self._path_aliases[text] = f"/redacted/path-{len(self._path_aliases) + 1:02d}{suffix}"
        return self._path_aliases[text]

    def alias_device_name(self, value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return value
        if text not in self._device_name_aliases:
            self._device_name_aliases[text] = f"device-{len(self._device_name_aliases) + 1:02d}"
        return self._device_name_aliases[text]

    @staticmethod
    def alias_identifier(category: str, value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return value
        digest = hashlib.sha256(f"{category}:{text}".encode("utf-8")).hexdigest()[:12]
        return f"{category}-{digest}"

    @staticmethod
    def _secret_placeholder(category: str, value: Any) -> Any:
        if value in {None, ""}:
            return value
        return f"REDACTED-{category.upper()}"


class SystemBackupService:
    def __init__(self, history_settings: HistorySettings, store: HistoryStore) -> None:
        self.history_settings = history_settings
        self.store = store

    def validate_scheduled_backup_scope(self, included_paths: list[str]) -> None:
        selected_groups = self._resolve_selected_groups(
            included_paths,
            bundle_type="backup",
        )
        self._validate_encrypted_scope(selected_groups, encrypt=True)

    def export_bundle(
        self,
        *,
        encrypt: bool = False,
        passphrase: str | None = None,
        packaging: ArchivePackaging = "tar.zst",
        included_paths: list[str] | None = None,
    ) -> BackupArtifact:
        artifact = self.export_bundle_to_file(
            encrypt=encrypt,
            passphrase=passphrase,
            packaging=packaging,
            included_paths=included_paths,
        )
        try:
            if artifact.path.stat().st_size > MAX_BACKUP_ARCHIVE_BYTES:
                raise ValueError(
                    "Backup bundle exceeds the in-memory limit; use the file-backed export API."
                )
            return BackupArtifact(
                filename=artifact.filename,
                content=artifact.path.read_bytes(),
                media_type=artifact.media_type,
                manifest=artifact.manifest,
            )
        finally:
            artifact.cleanup()

    def export_bundle_to_file(
        self,
        *,
        encrypt: bool = False,
        passphrase: str | None = None,
        packaging: ArchivePackaging = "tar.zst",
        included_paths: list[str] | None = None,
    ) -> FileBackupArtifact:
        return self._export_bundle_to_file(
            encrypt=encrypt,
            passphrase=passphrase,
            packaging=packaging,
            included_paths=included_paths,
            encrypted_outer_envelope=False,
        )

    def _export_bundle_to_file(
        self,
        *,
        encrypt: bool = False,
        passphrase: str | None = None,
        packaging: ArchivePackaging = "tar.zst",
        included_paths: list[str] | None = None,
        encrypted_outer_envelope: bool,
    ) -> FileBackupArtifact:
        app_settings = self._load_app_settings()
        exported_at = datetime.now(timezone.utc)
        selected_groups = self._resolve_selected_groups(included_paths, bundle_type="backup")
        self._validate_encrypted_scope(
            selected_groups,
            encrypt=encrypt or encrypted_outer_envelope,
        )
        requested_packaging = self._normalize_packaging(packaging)
        if encrypt and not passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        normalized_packaging: ArchivePackaging = "7z" if encrypt else requested_packaging
        workspace = Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-export-"))
        try:
            segmented_snapshot: _SegmentedExportSnapshot | None = None
            history_snapshot_path: Path | None = None
            if HISTORY_DB_KEY in selected_groups:
                if self.store.segment_catalog_path is not None:
                    segmented_snapshot = self._build_segmented_history_snapshot_to_directory(
                        workspace / "history-snapshot"
                    )
                    history_snapshot_path = segmented_snapshot.hot_path
                else:
                    history_snapshot_path = self._build_history_snapshot_to_directory(
                        workspace / "history-snapshot"
                    )
            bundle_groups, bundle_members = self._collect_backup_bundle(
                app_settings,
                history_snapshot_path,
                selected_groups=selected_groups,
            )
            if segmented_snapshot is not None:
                for segment_path in segmented_snapshot.segment_paths:
                    segment_id = segment_path.stem
                    bundle_members.append(
                        BundleMember(
                            key=f"history-segment:{segment_id}",
                            group_key=HISTORY_SEGMENT_GROUP_KEY,
                            archive_path=f"history/segments/{segment_id}.sqlite3",
                            source_path=None,
                            present=True,
                            content=None,
                            file_path=segment_path,
                        )
                    )
            manifest = self._build_manifest(
                format_name=BUNDLE_FORMAT,
                app_settings=app_settings,
                exported_at=exported_at,
                packaging=normalized_packaging,
                bundle_groups=bundle_groups,
                bundle_members=bundle_members,
            )
            if segmented_snapshot is not None:
                local_segments = segmented_snapshot.catalog.get("segments")
                if not isinstance(local_segments, list):
                    raise ValueError("Segmented history catalog is invalid.")
                manifest_segments: list[dict[str, Any]] = []
                for sequence, entry in enumerate(local_segments, start=1):
                    if not isinstance(entry, dict):
                        raise ValueError("Segmented history catalog is invalid.")
                    segment_id = entry.get("segment_id")
                    manifest_segments.append(
                        {
                            **entry,
                            "sequence": sequence,
                            "member_key": f"history-segment:{segment_id}",
                            "archive_path": f"history/segments/{segment_id}.sqlite3",
                            "schema_version": 1,
                            "required": True,
                            "supersedes": list(entry.get("supersedes") or []),
                        }
                    )
                generation_id = segmented_snapshot.catalog.get("generation_id")
                manifest.update(
                    {
                        "schema_version": SEGMENTED_BACKUP_SCHEMA_VERSION,
                        "generation": {
                            "generation_id": generation_id,
                            "complete": True,
                            "baseline": True,
                            "parent_generation_id": None,
                            "min_reader_version": SEGMENTED_BACKUP_SCHEMA_VERSION,
                        },
                        "history_catalog": {
                            "catalog_version": 1,
                            "hot_member_key": HISTORY_DB_KEY,
                            "segments": manifest_segments,
                            "tombstones": list(
                                segmented_snapshot.catalog.get("tombstones") or []
                            ),
                        },
                    }
                )
                validate_segmented_manifest(manifest)
            stem = f"jbod-system-backup-{exported_at.strftime('%Y%m%dT%H%M%SZ')}"
            filename = f"{stem}{ARCHIVE_FILE_SUFFIXES[normalized_packaging]}"
            archive_path = workspace / filename
            expanded_archive_size = self._build_archive_to_path(
                bundle_members,
                manifest,
                normalized_packaging,
                archive_path,
                passphrase=passphrase if encrypt else None,
            )
            self._validate_export_archive(
                archive_path,
                normalized_packaging,
                expanded_archive_size=expanded_archive_size,
                passphrase=passphrase if encrypt else None,
            )
            return FileBackupArtifact(
                filename=filename,
                path=archive_path,
                media_type=ARCHIVE_MEDIA_TYPES[normalized_packaging],
                manifest=manifest,
                cleanup_root=workspace,
            )
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    def export_scheduled_bundle_to_file(
        self,
        *,
        passphrase: str,
        included_paths: list[str] | None = None,
    ) -> FileBackupArtifact:
        if not passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        selected_groups = self._resolve_selected_groups(
            included_paths,
            bundle_type="backup",
        )
        uses_file_backed_history = HISTORY_DB_KEY in selected_groups
        artifact = self._export_bundle_to_file(
            encrypt=uses_file_backed_history,
            passphrase=passphrase if uses_file_backed_history else None,
            packaging="7z" if uses_file_backed_history else "tar.zst",
            included_paths=included_paths,
            encrypted_outer_envelope=not uses_file_backed_history,
        )
        if not uses_file_backed_history:
            encrypted_path = artifact.path.with_name(f"{artifact.path.name}.enc")
            try:
                self._encrypt_scheduled_archive(
                    artifact.path,
                    encrypted_path,
                    passphrase,
                )
                artifact.path.unlink(missing_ok=True)
                artifact = FileBackupArtifact(
                    filename=f"{artifact.filename}.enc",
                    path=encrypted_path,
                    media_type="application/octet-stream",
                    manifest=artifact.manifest,
                    cleanup_root=artifact.cleanup_root,
                )
            except Exception:
                artifact.cleanup()
                raise
        try:
            verification = self.preflight_scheduled_bundle_file(
                artifact.path,
                passphrase=passphrase,
                expected_groups=list(included_paths or []),
            )
            return FileBackupArtifact(
                filename=artifact.filename,
                path=artifact.path,
                media_type=artifact.media_type,
                manifest=verification["manifest"],
                cleanup_root=artifact.cleanup_root,
            )
        except Exception:
            artifact.cleanup()
            raise

    def _inspection_file_payload(
        self,
        manifest: dict[str, Any],
        extracted: dict[str, ExtractedMember],
        group_entries: dict[str, dict[str, Any]],
        group_key: str,
    ) -> bytes | None:
        group = group_entries.get(group_key)
        if (
            not self._manifest_group_selected(group)
            or not self._manifest_group_present(group)
        ):
            return None
        member = self._first_group_member(manifest, group_key)
        if member is None:
            return None
        return self._extracted_member_bytes(extracted[member["key"]])

    @staticmethod
    def _inspection_history_member_counts(
        content: ExtractedMember,
        *,
        include_tracked_slots: bool,
    ) -> dict[str, int]:
        temporary_path: Path | None = None
        try:
            if isinstance(content, bytes):
                descriptor, raw_path = tempfile.mkstemp(
                    prefix="truenas-jbod-ui-inspect-history-",
                    suffix=".sqlite3",
                )
                temporary_path = Path(raw_path)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary_path, 0o600)
                resolved_path = temporary_path
            else:
                resolved_path = content.resolve(strict=True)
            connection = sqlite3.connect(
                f"file:{resolved_path}?mode=ro&immutable=1",
                uri=True,
            )
            with closing(connection):
                return {
                    "tracked_slots": (
                        int(
                            connection.execute(
                                "SELECT COUNT(*) FROM slot_state_current"
                            ).fetchone()[0]
                        )
                        if include_tracked_slots
                        else 0
                    ),
                    "event_count": int(
                        connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0]
                    ),
                    "metric_sample_count": int(
                        connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
                    ),
                    "metric_rollup_count": int(
                        connection.execute("SELECT COUNT(*) FROM metric_rollups").fetchone()[0]
                    ),
                }
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _inspection_aggregate_counts(
        self,
        manifest: dict[str, Any],
        extracted: dict[str, ExtractedMember],
        group_entries: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        config_content = self._inspection_file_payload(
            manifest, extracted, group_entries, CONFIG_FILE_KEY
        )
        runtime_overrides_content = self._inspection_file_payload(
            manifest, extracted, group_entries, RUNTIME_OVERRIDES_FILE_KEY
        )
        profiles_content = self._inspection_file_payload(
            manifest, extracted, group_entries, PROFILE_FILE_KEY
        )
        mapping_content = self._inspection_file_payload(
            manifest, extracted, group_entries, MAPPING_FILE_KEY
        )
        alias_content = self._inspection_file_payload(
            manifest, extracted, group_entries, SAS_FABRIC_ALIAS_FILE_KEY
        )
        slot_detail_content = self._inspection_file_payload(
            manifest, extracted, group_entries, SLOT_DETAIL_FILE_KEY
        )

        system_count: int | None = None
        storage_view_count: int | None = None
        if config_content is not None:
            merged_config = _deep_merge(
                Settings().model_dump(),
                self._load_yaml_mapping(config_content),
            )
            if runtime_overrides_content is not None:
                merged_config = _deep_merge(
                    merged_config,
                    self._load_yaml_mapping(runtime_overrides_content),
                )
            if profiles_content is not None:
                profile_payload = yaml.safe_load(profiles_content.decode("utf-8")) or {}
                inspected_profiles = (
                    profile_payload
                    if isinstance(profile_payload, list)
                    else profile_payload.get("profiles")
                    if isinstance(profile_payload, dict)
                    else []
                )
                merged_config["profiles"] = [
                    *(merged_config.get("profiles") or []),
                    *(inspected_profiles or []),
                ]
            inspected_settings = _normalize_systems(Settings.model_validate(merged_config))
            profile_registry = ProfileRegistry(inspected_settings)
            system_count = len(inspected_settings.systems)
            storage_view_count = sum(
                len(resolve_system_storage_views(system, profile_registry))
                for system in inspected_settings.systems
            )

        profile_count: int | None = None
        if profiles_content is not None:
            profile_payload = yaml.safe_load(profiles_content.decode("utf-8")) or {}
            profiles = (
                profile_payload
                if isinstance(profile_payload, list)
                else profile_payload.get("profiles")
                if isinstance(profile_payload, dict)
                else None
            )
            profile_count = len(profiles) if isinstance(profiles, list) else 0

        def json_entry_count(content: bytes | None, key: str) -> int | None:
            if content is None:
                return None
            entries = self._load_json_mapping(content).get(key)
            return len(entries) if isinstance(entries, dict) else 0

        history_counts: dict[str, int] | None = None
        history_group = group_entries.get(HISTORY_DB_KEY)
        if (
            self._manifest_group_selected(history_group)
            and self._manifest_group_present(history_group)
        ):
            history_member = self._first_group_member(manifest, HISTORY_DB_KEY)
            if history_member is not None:
                history_counts = self._inspection_history_member_counts(
                    extracted[history_member["key"]],
                    include_tracked_slots=True,
                )
                history_catalog = manifest.get("history_catalog")
                segments = (
                    history_catalog.get("segments")
                    if isinstance(history_catalog, dict)
                    else []
                )
                for segment in segments or []:
                    if not isinstance(segment, dict):
                        continue
                    member_key = segment.get("member_key")
                    if not isinstance(member_key, str) or member_key not in extracted:
                        continue
                    segment_counts = self._inspection_history_member_counts(
                        extracted[member_key],
                        include_tracked_slots=False,
                    )
                    for key, value in segment_counts.items():
                        history_counts[key] += value

        def directory_member_count(group_key: str) -> int | None:
            group = group_entries.get(group_key)
            if not self._manifest_group_selected(group):
                return None
            if not self._manifest_group_present(group):
                return 0
            return len(self._group_members(manifest, group_key))

        return {
            "systems": system_count,
            "profiles": profile_count,
            "storage_views": storage_view_count,
            "mappings": json_entry_count(mapping_content, "slot_mappings"),
            "sas_fabric_aliases": json_entry_count(alias_content, "sas_fabric_aliases"),
            "slot_details": json_entry_count(slot_detail_content, "slot_details"),
            "ssh_keys": directory_member_count(SSH_KEYS_KEY),
            "tls_files": directory_member_count(TLS_TRUST_KEY),
            "known_hosts": directory_member_count(KNOWN_HOSTS_KEY),
            "history": history_counts,
        }

    def inspect_bundle_file(
        self,
        archive_path: str | Path,
        *,
        passphrase: str | None = None,
    ) -> dict[str, Any]:
        manifest, extracted, detected_packaging, archive_meta = self._read_archive_file(
            Path(archive_path),
            passphrase=passphrase,
        )
        cleanup_root = archive_meta.pop("_cleanup_root", None)
        try:
            group_entries = self._manifest_group_entries(manifest)
            selected_groups = [
                key
                for key, entry in group_entries.items()
                if self._manifest_group_selected(entry)
            ]
            present_groups = [
                key
                for key in selected_groups
                if self._manifest_group_present(group_entries.get(key))
            ]
            absent_groups = [
                key
                for key in selected_groups
                if not self._manifest_group_present(group_entries.get(key))
            ]
            self._validate_manifest_member_metadata(manifest, extracted)
            self._preflight_selected_group_members(manifest, group_entries, extracted)
            self._preflight_import_members(manifest, group_entries, extracted)
            return {
                "ok": True,
                "schema_version": manifest.get("schema_version"),
                "app_version": manifest.get("app_version"),
                "exported_at": manifest.get("exported_at"),
                "encrypted": bool(archive_meta.get("encrypted")),
                "packaging": manifest.get("packaging") or detected_packaging,
                "selected_groups": selected_groups,
                "present_groups": present_groups,
                "absent_groups": absent_groups,
                "member_count": len(extracted),
                "total_uncompressed_bytes": sum(
                    self._extracted_member_size(content)
                    for content in extracted.values()
                ),
                "aggregate_counts": self._inspection_aggregate_counts(
                    manifest,
                    extracted,
                    group_entries,
                ),
            }
        finally:
            self._cleanup_extracted_archive(cleanup_root)

    def preflight_scheduled_bundle_file(
        self,
        archive_path: str | Path,
        *,
        passphrase: str,
        expected_groups: list[str],
    ) -> dict[str, Any]:
        manifest, extracted, _packaging, archive_meta = self._read_archive_file(
            Path(archive_path),
            passphrase=passphrase,
        )
        cleanup_root = archive_meta.pop("_cleanup_root", None)
        try:
            if not archive_meta.get("encrypted"):
                raise ValueError("Scheduled backup encryption could not be verified.")
            group_entries = self._manifest_group_entries(manifest)
            selected_groups = [
                key
                for key, entry in group_entries.items()
                if self._manifest_group_selected(entry)
            ]
            if set(selected_groups) != set(expected_groups) or len(selected_groups) != len(expected_groups):
                raise ValueError("Scheduled backup selected groups do not match its configuration.")
            self._validate_manifest_member_metadata(manifest, extracted)
            self._preflight_selected_group_members(manifest, group_entries, extracted)
            self._preflight_import_members(manifest, group_entries, extracted)
            absent_groups = [
                key
                for key in expected_groups
                if not self._manifest_group_present(group_entries.get(key))
            ]
            return {
                "manifest": manifest,
                "selected_groups": selected_groups,
                "absent_groups": absent_groups,
            }
        finally:
            self._cleanup_extracted_archive(cleanup_root)

    @staticmethod
    def _scheduled_backup_key(passphrase: str, salt: bytes) -> bytes:
        return Scrypt(
            salt=salt,
            length=32,
            n=ENCRYPTED_BACKUP_SCRYPT_N,
            r=ENCRYPTED_BACKUP_SCRYPT_R,
            p=ENCRYPTED_BACKUP_SCRYPT_P,
        ).derive(passphrase.encode("utf-8"))

    @classmethod
    def _encrypt_scheduled_archive(
        cls,
        source_path: Path,
        output_path: Path,
        passphrase: str,
    ) -> None:
        salt = os.urandom(ENCRYPTED_BACKUP_SALT_BYTES)
        nonce = os.urandom(ENCRYPTED_BACKUP_NONCE_BYTES)
        header = ENCRYPTED_BACKUP_MAGIC + salt + nonce
        key = cls._scheduled_backup_key(passphrase, salt)
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(header)
        with source_path.open("rb") as source, output_path.open("wb") as output:
            output.write(header)
            while chunk := source.read(ARCHIVE_READ_CHUNK_BYTES):
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(output_path, 0o600)
        if output_path.stat().st_size > MAX_BACKUP_ARCHIVE_BYTES:
            raise ValueError(
                f"Backup bundle archive exceeds the {MAX_BACKUP_ARCHIVE_BYTES}-byte input limit."
            )

    @classmethod
    def _decrypt_scheduled_archive(cls, archive_bytes: bytes, passphrase: str | None) -> bytes:
        header_size = (
            len(ENCRYPTED_BACKUP_MAGIC)
            + ENCRYPTED_BACKUP_SALT_BYTES
            + ENCRYPTED_BACKUP_NONCE_BYTES
        )
        if len(archive_bytes) < header_size + ENCRYPTED_BACKUP_TAG_BYTES:
            raise ValueError("Scheduled backup archive is corrupted.")
        if not passphrase:
            raise ValueError("A passphrase is required for this encrypted backup bundle.")
        header = archive_bytes[:header_size]
        salt_start = len(ENCRYPTED_BACKUP_MAGIC)
        nonce_start = salt_start + ENCRYPTED_BACKUP_SALT_BYTES
        salt = header[salt_start:nonce_start]
        nonce = header[nonce_start:]
        ciphertext = archive_bytes[header_size:-ENCRYPTED_BACKUP_TAG_BYTES]
        tag = archive_bytes[-ENCRYPTED_BACKUP_TAG_BYTES:]
        key = cls._scheduled_backup_key(passphrase, salt)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        try:
            return decryptor.update(ciphertext) + decryptor.finalize()
        except InvalidTag as exc:
            raise ValueError("Scheduled backup archive could not be decrypted.") from exc

    def export_debug_bundle(
        self,
        *,
        encrypt: bool = False,
        passphrase: str | None = None,
        packaging: ArchivePackaging = "tar.zst",
        included_paths: list[str] | None = None,
        scrub_secrets: bool = True,
        scrub_disk_identifiers: bool = True,
        runtime_payload: dict[str, Any] | None = None,
        maintenance_payload: dict[str, Any] | None = None,
    ) -> BackupArtifact:
        artifact = self.export_debug_bundle_to_file(
            encrypt=encrypt,
            passphrase=passphrase,
            packaging=packaging,
            included_paths=included_paths,
            scrub_secrets=scrub_secrets,
            scrub_disk_identifiers=scrub_disk_identifiers,
            runtime_payload=runtime_payload,
            maintenance_payload=maintenance_payload,
        )
        try:
            if artifact.path.stat().st_size > MAX_BACKUP_ARCHIVE_BYTES:
                raise ValueError(
                    "Debug bundle exceeds the in-memory limit; use the file-backed export API."
                )
            return BackupArtifact(
                filename=artifact.filename,
                content=artifact.path.read_bytes(),
                media_type=artifact.media_type,
                manifest=artifact.manifest,
            )
        finally:
            artifact.cleanup()

    def export_debug_bundle_to_file(
        self,
        *,
        encrypt: bool = False,
        passphrase: str | None = None,
        packaging: ArchivePackaging = "tar.zst",
        included_paths: list[str] | None = None,
        scrub_secrets: bool = True,
        scrub_disk_identifiers: bool = True,
        runtime_payload: dict[str, Any] | None = None,
        maintenance_payload: dict[str, Any] | None = None,
    ) -> FileBackupArtifact:
        app_settings = self._load_app_settings()
        exported_at = datetime.now(timezone.utc)
        selected_groups = self._resolve_selected_groups(included_paths, bundle_type="debug")
        sensitive_selection = [key for key in selected_groups if key in SENSITIVE_GROUP_KEYS]
        if scrub_secrets and sensitive_selection:
            labels = ", ".join(BACKUP_GROUP_METADATA[key]["label"] for key in sensitive_selection)
            raise ValueError(
                f"Debug bundles cannot include locked secret paths ({labels}) while secret scrubbing is enabled. Deselect them or turn secret scrubbing off."
            )
        self._validate_encrypted_scope(selected_groups, encrypt=encrypt)
        requested_packaging = self._normalize_packaging(packaging)
        if encrypt and not passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        normalized_packaging: ArchivePackaging = "7z" if encrypt else requested_packaging
        scrubber = (
            DebugScrubber(
                scrub_secrets=scrub_secrets,
                scrub_disk_identifiers=scrub_disk_identifiers,
            )
            if scrub_secrets or scrub_disk_identifiers
            else None
        )
        workspace = Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-debug-export-"))
        try:
            segmented_snapshot: _SegmentedExportSnapshot | None = None
            history_snapshot_path: Path | None = None
            history_segment_paths: tuple[Path, ...] = ()
            if HISTORY_DB_KEY in selected_groups:
                if self.store.segment_catalog_path is not None:
                    segmented_snapshot = self._build_segmented_history_snapshot_to_directory(
                        workspace / "history-snapshot"
                    )
                    history_snapshot_path = segmented_snapshot.hot_path
                    history_segment_paths = segmented_snapshot.segment_paths
                else:
                    history_snapshot_path = self._build_history_snapshot_to_directory(
                        workspace / "history-snapshot"
                    )
            if history_snapshot_path is not None and scrubber is not None:
                history_snapshot_path = self._build_scrubbed_history_snapshot_file(
                    history_snapshot_path,
                    scrubber,
                    workspace / "history-scrub.sqlite3",
                )
                if history_segment_paths:
                    scrubbed_segment_root = workspace / "history-segment-scrub"
                    scrubbed_segment_root.mkdir(mode=0o700)
                    history_segment_paths = tuple(
                        self._build_scrubbed_history_snapshot_file(
                            segment_path,
                            scrubber,
                            scrubbed_segment_root / segment_path.name,
                        )
                        for segment_path in history_segment_paths
                    )
            bundle_groups, bundle_members = self._collect_debug_bundle(
                app_settings,
                history_snapshot_path,
                selected_groups=selected_groups,
                scrubber=scrubber,
                runtime_payload=runtime_payload,
                maintenance_payload=maintenance_payload,
                exported_at=exported_at,
            )
            if segmented_snapshot is not None:
                bundle_members.append(
                    BundleMember(
                        key="history-segment-catalog",
                        group_key=HISTORY_DB_KEY,
                        archive_path="history/segments/catalog.json",
                        source_path=None,
                        present=True,
                        content=self._build_debug_segment_catalog_bytes(
                            segmented_snapshot.catalog,
                            history_segment_paths,
                        ),
                    )
                )
                for segment_path in history_segment_paths:
                    segment_id = segment_path.stem
                    bundle_members.append(
                        BundleMember(
                            key=f"history-segment:{segment_id}",
                            group_key=HISTORY_DB_KEY,
                            archive_path=f"history/segments/{segment_id}.sqlite3",
                            source_path=None,
                            present=True,
                            content=None,
                            file_path=segment_path,
                        )
                    )
            manifest = self._build_manifest(
                format_name=DEBUG_BUNDLE_FORMAT,
                app_settings=app_settings,
                exported_at=exported_at,
                packaging=normalized_packaging,
                bundle_groups=bundle_groups,
                bundle_members=bundle_members,
                extra_fields={
                    "scrub_sensitive": scrub_secrets or scrub_disk_identifiers,
                    "scrub_secrets": scrub_secrets,
                    "scrub_disk_identifiers": scrub_disk_identifiers,
                },
            )
            stem = f"jbod-debug-bundle-{exported_at.strftime('%Y%m%dT%H%M%SZ')}"
            filename = f"{stem}{ARCHIVE_FILE_SUFFIXES[normalized_packaging]}"
            archive_path = workspace / filename
            expanded_archive_size = self._build_archive_to_path(
                bundle_members,
                manifest,
                normalized_packaging,
                archive_path,
                passphrase=passphrase if encrypt else None,
            )
            self._validate_export_archive(
                archive_path,
                normalized_packaging,
                expanded_archive_size=expanded_archive_size,
                passphrase=passphrase if encrypt else None,
            )
            return FileBackupArtifact(
                filename=filename,
                path=archive_path,
                media_type=ARCHIVE_MEDIA_TYPES[normalized_packaging],
                manifest=manifest,
                cleanup_root=workspace,
            )
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    @staticmethod
    def _create_segmented_activation_marker(
        store: HistoryStore,
        payload: dict[str, Any],
    ) -> tuple[Path, dict[str, Any]]:
        marker_path = activation_pending_path(store.file_path)
        return marker_path, write_restore_journal(marker_path, payload)

    @staticmethod
    def _remove_segmented_activation_marker(
        marker: tuple[Path, dict[str, Any]],
    ) -> None:
        marker_path, marker_record = marker
        remove_recorded_restore_file(
            marker_path,
            marker_record,
            label="activation journal",
        )

    def import_bundle(self, content: bytes, *, passphrase: str | None = None) -> dict[str, Any]:
        if len(content) > MAX_BACKUP_ARCHIVE_BYTES:
            raise ValueError(
                f"Backup bundle archive exceeds the {MAX_BACKUP_ARCHIVE_BYTES}-byte input limit."
            )
        return self._import_parsed_bundle(
            self._read_archive(content, passphrase=passphrase)
        )

    def import_bundle_from_file(
        self,
        archive_path: str | Path,
        *,
        passphrase: str | None = None,
    ) -> dict[str, Any]:
        return self._import_parsed_bundle(
            self._read_archive_file(Path(archive_path), passphrase=passphrase)
        )

    def _import_parsed_bundle(
        self,
        parsed: tuple[
            dict[str, Any],
            dict[str, ExtractedMember],
            ArchivePackaging,
            dict[str, Any],
        ],
    ) -> dict[str, Any]:
        manifest, extracted, detected_packaging, archive_meta = parsed
        cleanup_root = archive_meta.pop("_cleanup_root", None)
        try:
            group_entries = self._manifest_group_entries(manifest)
            self._validate_manifest_member_metadata(manifest, extracted)
            self._preflight_selected_group_members(manifest, group_entries, extracted)
            self._preflight_import_members(manifest, group_entries, extracted)
            self._prepare_segmented_history_import(manifest, extracted)
            segmented_restore = manifest.get("schema_version") == SEGMENTED_BACKUP_SCHEMA_VERSION
            segmented_activation = (
                self._segmented_history_activation_members(
                    manifest,
                    extracted,
                    group_entries,
                )
                if segmented_restore
                else None
            )
            lock_context = (
                history_write_lock(self.store.file_path, blocking=False)
                if segmented_restore
                else nullcontext()
            )
            transaction = _ImportActivationTransaction(
                extracted,
                history_store=self.store,
            )
            marker: tuple[Path, dict[str, Any]] | None = None
            try:
                with lock_context:
                    try:
                        with transaction:
                            if segmented_activation is not None:
                                generation = manifest.get("generation")
                                generation_id = (
                                    generation.get("generation_id")
                                    if isinstance(generation, dict)
                                    else None
                                )
                                marker_payload = transaction.prepare_segmented_history(
                                    self.store,
                                    hot_member_key=segmented_activation[0],
                                    segment_members=segmented_activation[1],
                                    generation_id=(
                                        str(generation_id)
                                        if generation_id is not None
                                        else None
                                    ),
                                )
                                marker = self._create_segmented_activation_marker(
                                    self.store,
                                    marker_payload,
                                )
                            extraction_root = cleanup_root
                            cleanup_root = None
                            self._cleanup_extracted_archive(extraction_root)
                            result = self._activate_import_bundle(
                                manifest,
                                extracted,
                                group_entries,
                                detected_packaging,
                                archive_meta,
                                transaction,
                            )
                            transaction.commit()
                    except Exception:
                        if marker is not None:
                            if transaction.rollback_completed:
                                self._remove_segmented_activation_marker(marker)
                            marker = None
                        raise
                    if marker is not None:
                        self._remove_segmented_activation_marker(marker)
                        marker = None
                    return result
            except Exception:
                get_settings.cache_clear()
                raise
        finally:
            self._cleanup_extracted_archive(cleanup_root)

    def _prepare_segmented_history_import(
        self,
        manifest: dict[str, Any],
        extracted: dict[str, ExtractedMember],
    ) -> None:
        if manifest.get("schema_version") != SEGMENTED_BACKUP_SCHEMA_VERSION:
            return
        history_catalog = manifest.get("history_catalog")
        generation = manifest.get("generation")
        if not isinstance(history_catalog, dict) or not isinstance(generation, dict):
            raise ValueError("Segmented history manifest is invalid.")
        manifest_segments = history_catalog.get("segments")
        if not isinstance(manifest_segments, list):
            raise ValueError("Segmented history manifest is invalid.")
        local_segments: list[dict[str, Any]] = []
        for entry in manifest_segments:
            if not isinstance(entry, dict):
                raise ValueError("Segmented history manifest is invalid.")
            member_key = entry.get("member_key")
            if not isinstance(member_key, str) or member_key not in extracted:
                raise ValueError("Segmented history archive is missing a required segment.")
            self._validate_history_member(extracted[member_key])
            segment_id = entry.get("segment_id")
            local_segments.append(
                {
                    "segment_id": segment_id,
                    "file_name": f"{segment_id}.sqlite3",
                    "sha256": entry.get("sha256"),
                    "size_bytes": entry.get("size_bytes"),
                    "coverage_start": entry.get("coverage_start"),
                    "coverage_end": entry.get("coverage_end"),
                    "sealed_at": entry.get("sealed_at"),
                    "key_id": entry.get("key_id"),
                    "row_counts": entry.get("row_counts"),
                    "supersedes": list(entry.get("supersedes") or []),
                }
            )
        local_catalog = {
            "catalog_version": history_catalog.get("catalog_version"),
            "generation_id": generation.get("generation_id"),
            "complete": generation.get("complete"),
            "segments": local_segments,
            "tombstones": list(history_catalog.get("tombstones") or []),
        }
        extracted[SEGMENTED_CATALOG_STAGING_KEY] = json.dumps(
            local_catalog,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _segmented_history_activation_members(
        self,
        manifest: dict[str, Any],
        extracted: dict[str, ExtractedMember],
        group_entries: dict[str, dict[str, Any]],
    ) -> tuple[str, list[tuple[str, Path]]] | None:
        history_group = group_entries.get(HISTORY_DB_KEY)
        if not self._manifest_group_selected(history_group):
            return None
        history_member = self._first_group_member(manifest, HISTORY_DB_KEY)
        if history_member is None or history_member["key"] not in extracted:
            if self._manifest_group_present(history_group):
                raise ValueError("Backup bundle is missing the selected history database member.")
            return None
        history_catalog = manifest.get("history_catalog")
        if not isinstance(history_catalog, dict):
            raise ValueError("Segmented history manifest is invalid.")
        manifest_segments = history_catalog.get("segments")
        if not isinstance(manifest_segments, list):
            raise ValueError("Segmented history manifest is invalid.")
        segment_members: list[tuple[str, Path]] = []
        for entry in manifest_segments:
            if not isinstance(entry, dict):
                raise ValueError("Segmented history manifest is invalid.")
            segment_id = entry.get("segment_id")
            member_key = entry.get("member_key")
            if (
                not isinstance(segment_id, str)
                or not isinstance(member_key, str)
                or member_key not in extracted
            ):
                raise ValueError("Segmented history manifest is invalid.")
            segment_members.append((member_key, Path(f"{segment_id}.sqlite3")))
        if SEGMENTED_CATALOG_STAGING_KEY not in extracted:
            raise ValueError("Segmented history catalog staging member is missing.")
        segment_members.append((SEGMENTED_CATALOG_STAGING_KEY, Path("catalog.json")))
        return str(history_member["key"]), segment_members

    def _activate_import_bundle(
        self,
        manifest: dict[str, Any],
        extracted: dict[str, ExtractedMember],
        group_entries: dict[str, dict[str, Any]],
        detected_packaging: ArchivePackaging,
        archive_meta: dict[str, Any],
        transaction: _ImportActivationTransaction,
    ) -> dict[str, Any]:
        restored_paths: list[str] = []
        preserved_absent_groups = [
            key
            for key in BACKUP_GROUP_METADATA
            if self._manifest_group_selected(group_entries.get(key))
            and not self._manifest_group_present(group_entries.get(key))
        ]
        app_settings = self._load_app_settings()

        self._restore_file_group(
            CONFIG_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(app_settings.config_file),
            restored_paths,
            transaction,
        )

        imported_settings = self._load_app_settings()
        self._restore_file_group(
            RUNTIME_OVERRIDES_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.runtime_overrides_file),
            restored_paths,
            transaction,
        )

        imported_settings = self._load_app_settings()
        self._restore_file_group(
            PROFILE_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.profile_file),
            restored_paths,
            transaction,
        )
        self._restore_file_group(
            MAPPING_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.mapping_file),
            restored_paths,
            transaction,
        )
        self._restore_file_group(
            SAS_FABRIC_ALIAS_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.sas_fabric_alias_file),
            restored_paths,
            transaction,
        )
        self._restore_file_group(
            SLOT_DETAIL_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.slot_detail_cache_file),
            restored_paths,
            transaction,
        )

        imported_settings = self._load_app_settings()
        config_root = Path(imported_settings.config_file).parent
        self._restore_directory_group(
            SSH_KEYS_KEY,
            manifest,
            group_entries,
            extracted,
            config_root / "ssh",
            restored_paths,
            transaction,
        )
        self._restore_directory_group(
            TLS_TRUST_KEY,
            manifest,
            group_entries,
            extracted,
            config_root / "tls",
            restored_paths,
            transaction,
        )
        known_hosts_target = Path(_derive_runtime_layout_paths(imported_settings.config_file)["known_hosts_path"])
        self._restore_file_group(
            KNOWN_HOSTS_KEY,
            manifest,
            group_entries,
            extracted,
            known_hosts_target,
            restored_paths,
            transaction,
        )

        history_restored = False
        history_group = group_entries.get(HISTORY_DB_KEY)
        if self._manifest_group_selected(history_group):
            history_member = self._first_group_member(manifest, HISTORY_DB_KEY)
            if history_member and history_member["key"] in extracted:
                if manifest.get("schema_version") == SEGMENTED_BACKUP_SCHEMA_VERSION:
                    segmented_activation = self._segmented_history_activation_members(
                        manifest,
                        extracted,
                        group_entries,
                    )
                    if segmented_activation is None:
                        raise ValueError("Segmented history activation members are missing.")
                    transaction.activate_segmented_history(
                        self.store,
                        hot_member_key=segmented_activation[0],
                        segment_members=segmented_activation[1],
                    )
                else:
                    transaction.activate_history(self.store, history_member["key"])
                restored_paths.append(str(self.store.file_path))
                if self.store.segment_catalog_path is not None:
                    restored_paths.append(str(self.store.segment_catalog_path.parent))
                history_restored = True
            elif self._manifest_group_present(history_group):
                raise ValueError("Backup bundle is missing the selected history database member.")

        imported_settings = self._load_app_settings()
        return {
            "ok": True,
            "schema_version": manifest.get("schema_version"),
            "app_version": manifest.get("app_version"),
            "exported_at": manifest.get("exported_at"),
            "encrypted": bool(archive_meta.get("encrypted")),
            "packaging": manifest.get("packaging") or detected_packaging,
            "default_system_id": imported_settings.default_system_id,
            "system_count": len(imported_settings.systems),
            "systems": [
                {
                    "id": system.id,
                    "label": system.label,
                    "platform": system.truenas.platform,
                }
                for system in imported_settings.systems
            ],
            "included_groups": [
                key
                for key, entry in group_entries.items()
                if self._manifest_group_selected(entry)
            ],
            "restored_history_database": history_restored,
            "restored_paths": restored_paths,
            "preserved_absent_groups": preserved_absent_groups,
        }

    def _load_app_settings(self) -> Settings:
        get_settings.cache_clear()
        return get_settings()

    @staticmethod
    def _cleanup_extracted_archive(cleanup_root: Any) -> None:
        if cleanup_root is None:
            return
        root = Path(cleanup_root)
        if root.exists():
            try:
                shutil.rmtree(root)
            except OSError as exc:
                raise RuntimeError(
                    "Backup bundle extraction workspace cleanup failed."
                ) from exc

    def _preflight_import_members(
        self,
        manifest: dict[str, Any],
        group_entries: dict[str, dict[str, Any]],
        extracted_members: dict[str, ExtractedMember],
    ) -> None:
        validators: dict[str, Any] = {
            CONFIG_FILE_KEY: self._validate_config_member,
            RUNTIME_OVERRIDES_FILE_KEY: self._validate_runtime_overrides_member,
            PROFILE_FILE_KEY: self._validate_profile_member,
            MAPPING_FILE_KEY: self._validate_mapping_member,
            SAS_FABRIC_ALIAS_FILE_KEY: self._validate_sas_alias_member,
            SLOT_DETAIL_FILE_KEY: self._validate_slot_detail_member,
            HISTORY_DB_KEY: self._validate_history_member,
        }
        for group_key, validator in validators.items():
            group_entry = group_entries.get(group_key)
            if not self._manifest_group_selected(group_entry):
                continue
            member_entry = self._first_group_member(manifest, group_key)
            if member_entry is None:
                if self._manifest_group_present(group_entry):
                    raise ValueError(f"Backup bundle is missing the selected {group_key} member.")
                continue
            member_key = member_entry["key"]
            if member_key not in extracted_members:
                raise ValueError(f"Backup bundle is missing the selected {group_key} member.")
            try:
                content = extracted_members[member_key]
                if group_key == HISTORY_DB_KEY:
                    self._validate_history_member(content)
                else:
                    validator(self._extracted_member_bytes(content))
            except (UnicodeError, yaml.YAMLError, json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Backup bundle selected {group_key} member is invalid."
                ) from exc

    def _preflight_selected_group_members(
        self,
        manifest: dict[str, Any],
        group_entries: dict[str, dict[str, Any]],
        extracted_members: dict[str, ExtractedMember],
    ) -> None:
        for group_key, group_entry in group_entries.items():
            if not self._manifest_group_selected(group_entry):
                continue
            metadata = BACKUP_GROUP_METADATA.get(group_key)
            if metadata is None:
                raise ValueError(f"Backup bundle selected unsupported group {group_key!r}.")
            member_entries = self._group_members(manifest, group_key)
            present = self._manifest_group_present(group_entry)
            if present and not member_entries:
                raise ValueError(f"Backup bundle is missing the selected {group_key} member.")
            if not present and member_entries:
                raise ValueError(
                    f"Backup bundle selected {group_key} is marked absent but contains members."
                )
            if not present:
                continue
            restore_mode = str(metadata.get("restore_mode") or "")
            if restore_mode in {"file", "history_db"} and len(member_entries) != 1:
                raise ValueError(
                    f"Backup bundle selected {group_key} must contain exactly one member."
                )
            for member_entry in member_entries:
                member_key = member_entry["key"]
                if member_key not in extracted_members:
                    raise ValueError(
                        f"Backup bundle is missing the selected {group_key} member."
                    )
                if restore_mode == "directory":
                    self._directory_member_relative_path(
                        group_key,
                        member_entry["archive_path"],
                    )

    def _validate_manifest_member_metadata(
        self,
        manifest: dict[str, Any],
        extracted_members: dict[str, ExtractedMember],
    ) -> None:
        seen_keys: set[str] = set()
        seen_paths: set[str] = set()
        for index, raw_entry in enumerate(manifest.get("files", [])):
            if not isinstance(raw_entry, dict):
                continue
            raw_archive_path = str(raw_entry.get("archive_path") or "").strip()
            if not raw_archive_path:
                continue
            archive_path = self._normalize_archive_member_path(raw_archive_path)
            key = str(raw_entry.get("key") or archive_path or f"member-{index}").strip()
            if key in seen_keys:
                raise ValueError(f"Backup bundle manifest contains duplicate member key {key!r}.")
            if archive_path in seen_paths:
                raise ValueError(
                    f"Backup bundle manifest contains duplicate archive path {archive_path!r}."
                )
            seen_keys.add(key)
            seen_paths.add(archive_path)

            content = extracted_members.get(key)
            if content is None:
                raise ValueError(f"Backup bundle is missing {archive_path}.")

            expected_size = raw_entry.get("size_bytes")
            if expected_size is not None:
                if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
                    raise ValueError(
                        f"Backup bundle member {archive_path} has invalid size metadata."
                    )
                if self._extracted_member_size(content) != expected_size:
                    raise ValueError(
                        f"Backup bundle member {archive_path} size does not match its manifest."
                    )

            expected_sha256 = raw_entry.get("sha256")
            if expected_sha256 is not None:
                digest = str(expected_sha256).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError(
                        f"Backup bundle member {archive_path} has invalid SHA-256 metadata."
                    )
                if self._extracted_member_sha256(content) != digest:
                    raise ValueError(
                        f"Backup bundle member {archive_path} SHA-256 does not match its manifest."
                    )

    @staticmethod
    def _extracted_member_bytes(content: ExtractedMember) -> bytes:
        if isinstance(content, bytes):
            return content
        return content.read_bytes()

    @staticmethod
    def _extracted_member_size(content: ExtractedMember) -> int:
        if isinstance(content, bytes):
            return len(content)
        return content.stat(follow_symlinks=False).st_size

    @staticmethod
    def _extracted_member_sha256(content: ExtractedMember) -> str:
        if isinstance(content, bytes):
            return hashlib.sha256(content).hexdigest()
        digest = hashlib.sha256()
        with content.open("rb", buffering=0) as source:
            while chunk := source.read(ARCHIVE_READ_CHUNK_BYTES):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _load_yaml_mapping(content: bytes) -> dict[str, Any]:
        payload = yaml.safe_load(content.decode("utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError("YAML payload must contain a mapping.")
        return payload

    @staticmethod
    def _load_json_mapping(content: bytes) -> dict[str, Any]:
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON payload must contain an object.")
        return payload

    @classmethod
    def _validate_config_member(cls, content: bytes) -> None:
        Settings.model_validate(cls._load_yaml_mapping(content))

    @classmethod
    def _validate_runtime_overrides_member(cls, content: bytes) -> None:
        payload = cls._load_yaml_mapping(content)
        app_payload = payload.get("app", {})
        if not isinstance(app_payload, dict):
            raise ValueError("Runtime override app payload must contain a mapping.")
        Settings.model_validate({"app": app_payload})

    @classmethod
    def _validate_profile_member(cls, content: bytes) -> None:
        payload = yaml.safe_load(content.decode("utf-8")) or {}
        if isinstance(payload, list):
            profiles = payload
        elif isinstance(payload, dict):
            profiles = payload.get("profiles", payload)
        else:
            profiles = None
        if not isinstance(profiles, list):
            raise ValueError(
                "Profile payload must contain a profile list or a mapping with a profiles list."
            )
        for profile in profiles:
            EnclosureProfileConfig.model_validate(profile)

    @classmethod
    def _validate_mapping_member(cls, content: bytes) -> None:
        payload = cls._load_json_mapping(content)
        mappings = payload.get("slot_mappings", {})
        if not isinstance(mappings, dict):
            raise ValueError("Mapping payload must contain a slot_mappings object.")
        for mapping in mappings.values():
            ManualMapping.model_validate(mapping)

    @classmethod
    def _validate_sas_alias_member(cls, content: bytes) -> None:
        payload = cls._load_json_mapping(content)
        aliases = payload.get("sas_fabric_aliases", {})
        if not isinstance(aliases, dict):
            raise ValueError("SAS alias payload must contain a sas_fabric_aliases object.")
        for alias in aliases.values():
            SasFabricAlias.model_validate(alias)

    @classmethod
    def _validate_slot_detail_member(cls, content: bytes) -> None:
        payload = cls._load_json_mapping(content)
        entries = payload.get("slot_details", {})
        if not isinstance(entries, dict):
            raise ValueError("Slot detail payload must contain a slot_details object.")
        for entry in entries.values():
            SlotDetailCacheEntry.model_validate(entry)

    @staticmethod
    def _validate_history_member(content: ExtractedMember) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            if isinstance(content, Path):
                candidate_path = content
            else:
                candidate_path = Path(temp_dir) / "history.sqlite3"
                candidate_path.write_bytes(content)
            database_uri = f"{candidate_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(database_uri, uri=True)) as connection:
                rows = connection.execute("PRAGMA quick_check").fetchall()
                required_columns = {
                    "slot_state_current": {"system_id", "enclosure_key", "slot"},
                    "slot_events": {"system_id", "enclosure_key", "slot", "observed_at"},
                    "metric_samples": {
                        "system_id",
                        "enclosure_key",
                        "slot",
                        "observed_at",
                        "metric_name",
                    },
                }
                for table_name, expected_columns in required_columns.items():
                    table_row = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table_name,),
                    ).fetchone()
                    if table_row is None:
                        raise ValueError(f"History database is missing table {table_name}.")
                    columns = {
                        str(row[1])
                        for row in connection.execute(
                            f'PRAGMA table_info("{table_name}")'
                        ).fetchall()
                    }
                    if not expected_columns.issubset(columns):
                        raise ValueError(
                            f"History database table {table_name} is missing required columns."
                        )
            if rows != [("ok",)]:
                raise ValueError("History database integrity check failed.")

    def _build_history_snapshot_to_directory(self, target_dir: Path) -> Path | None:
        backup_path = self.store.create_backup(
            target_dir,
            retention_count=1,
            long_term_backup_dir=None,
            weekly_retention_count=0,
            monthly_retention_count=0,
        )
        return Path(backup_path) if backup_path is not None else None

    def _build_segmented_history_snapshot_to_directory(
        self,
        target_dir: Path,
    ) -> _SegmentedExportSnapshot:
        catalog_path = self.store.segment_catalog_path
        if catalog_path is None:
            raise ValueError("Segmented history catalog is not configured.")
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        hot_path = target_dir / "history.sqlite3"
        segment_root = target_dir / "segments"
        segment_root.mkdir(mode=0o700)
        staged_segments: list[Path] = []
        catalog: dict[str, Any]
        source_segments: tuple[Path, ...]
        catalog_record: dict[str, Any]
        with history_write_lock(self.store.file_path, blocking=False):
            with self.store._lock:
                catalog_record = record_restore_file(catalog_path)
                reader = SegmentedHistoryReader.from_catalog(
                    hot_path=self.store.file_path,
                    catalog_path=catalog_path,
                )
                with closing(self.store._connect(migration_lock_held=True)) as source_connection, closing(
                    sqlite3.connect(hot_path)
                ) as snapshot_connection:
                    snapshot_connection.execute("PRAGMA journal_mode=DELETE")
                    source_connection.backup(snapshot_connection)
                    snapshot_connection.commit()
                os.chmod(hot_path, 0o600)
                with hot_path.open("rb", buffering=0) as stream:
                    os.fsync(stream.fileno())
                source_segments = reader.verify_catalog_segments()
                catalog = dict(reader.catalog_payload())
                if not restore_file_matches(catalog_path, catalog_record):
                    raise ValueError("Segmented history export catalog changed during verification.")
                referenced_segments = {path.absolute() for path in source_segments}
                live_hot_path = self.store.file_path.absolute()
                unreferenced_segments = [
                    path
                    for path in catalog_path.parent.iterdir()
                    if path.name.endswith(".sqlite3")
                    and not path.name.startswith(".")
                    and path.absolute() != live_hot_path
                    and path.absolute() not in referenced_segments
                ]
                if unreferenced_segments:
                    raise ValueError("Segmented history export contains an unreferenced segment file.")
        catalog_segments = catalog.get("segments")
        if not isinstance(catalog_segments, list):
            raise ValueError("Segmented history catalog is invalid.")
        expected_segments = {
            str(entry.get("file_name")): entry
            for entry in catalog_segments
            if isinstance(entry, dict)
        }
        for source_segment in source_segments:
            target_segment = segment_root / source_segment.name
            shutil.copyfile(source_segment, target_segment)
            os.chmod(target_segment, 0o600)
            with target_segment.open("rb", buffering=0) as stream:
                os.fsync(stream.fileno())
            expected = expected_segments.get(source_segment.name)
            if (
                expected is None
                or target_segment.stat().st_size != expected.get("size_bytes")
                or self._extracted_member_sha256(target_segment) != expected.get("sha256")
            ):
                raise ValueError("Segmented history export segment integrity check failed.")
            staged_segments.append(target_segment)
        if not restore_file_matches(catalog_path, catalog_record):
            raise ValueError("Segmented history export catalog changed during segment copy.")
        _ImportActivationTransaction._fsync_directory(segment_root)
        _ImportActivationTransaction._fsync_directory(target_dir)
        return _SegmentedExportSnapshot(
            hot_path=hot_path,
            catalog=catalog,
            segment_paths=tuple(staged_segments),
        )

    def _build_debug_segment_catalog_bytes(
        self,
        catalog: dict[str, Any],
        segment_paths: tuple[Path, ...],
    ) -> bytes:
        paths_by_name = {path.name: path for path in segment_paths}
        debug_segments: list[dict[str, Any]] = []
        for entry in catalog.get("segments") or []:
            segment_path = paths_by_name[str(entry["file_name"])]
            debug_segments.append(
                {
                    **entry,
                    "size_bytes": segment_path.stat().st_size,
                    "sha256": self._extracted_member_sha256(segment_path),
                }
            )
        debug_catalog = {**catalog, "segments": debug_segments}
        return json.dumps(debug_catalog, indent=2, sort_keys=True).encode("utf-8")

    def _resolve_selected_groups(
        self,
        requested_groups: list[str] | None,
        *,
        bundle_type: Literal["backup", "debug"],
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        allowed = {
            key
            for key, metadata in BACKUP_GROUP_METADATA.items()
            if bundle_type in metadata["bundle_types"]
        }
        if requested_groups:
            for raw_key in requested_groups:
                key = str(raw_key or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                normalized.append(key)
        if not normalized:
            normalized = (
                list(DEFAULT_BACKUP_GROUP_KEYS)
                if bundle_type == "backup"
                else list(DEFAULT_DEBUG_GROUP_KEYS)
            )
        invalid = [key for key in normalized if key not in allowed]
        if invalid:
            raise ValueError(f"Unsupported backup path selection: {', '.join(invalid)}")
        return normalized

    @staticmethod
    def _validate_encrypted_scope(selected_groups: list[str], *, encrypt: bool) -> None:
        sensitive = [key for key in selected_groups if key in SENSITIVE_GROUP_KEYS]
        if sensitive and not encrypt:
            labels = ", ".join(BACKUP_GROUP_METADATA[key]["label"] for key in sensitive)
            raise ValueError(f"Encrypted export is required when including locked secret paths ({labels}).")

    def _collect_backup_bundle(
        self,
        app_settings: Settings,
        history_snapshot_path: Path | None,
        *,
        selected_groups: list[str],
    ) -> tuple[list[BundleGroup], list[BundleMember]]:
        layout_paths = _derive_runtime_layout_paths(app_settings.config_file)
        config_root = Path(app_settings.config_file).parent
        bundle_groups: list[BundleGroup] = []
        bundle_members: list[BundleMember] = []

        for group_key in BACKUP_GROUP_METADATA:
            metadata = BACKUP_GROUP_METADATA[group_key]
            if "backup" not in metadata["bundle_types"]:
                continue
            selected = group_key in selected_groups
            if group_key == CONFIG_FILE_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(app_settings.config_file),
                    selected=selected,
                )
            elif group_key == RUNTIME_OVERRIDES_FILE_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(app_settings.paths.runtime_overrides_file),
                    selected=selected,
                )
            elif group_key == PROFILE_FILE_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(app_settings.paths.profile_file),
                    selected=selected,
                )
            elif group_key == MAPPING_FILE_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(app_settings.paths.mapping_file),
                    selected=selected,
                )
            elif group_key == SAS_FABRIC_ALIAS_FILE_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(app_settings.paths.sas_fabric_alias_file),
                    selected=selected,
                )
            elif group_key == SLOT_DETAIL_FILE_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(app_settings.paths.slot_detail_cache_file),
                    selected=selected,
                )
            elif group_key == HISTORY_DB_KEY:
                group, members = self._collect_generated_path_group(
                    group_key,
                    history_snapshot_path,
                    selected=selected,
                    source_path=str(self.store.file_path),
                )
            elif group_key == SSH_KEYS_KEY:
                group, members = self._collect_directory_group(
                    group_key,
                    config_root / "ssh",
                    selected=selected,
                )
            elif group_key == TLS_TRUST_KEY:
                group, members = self._collect_directory_group(
                    group_key,
                    config_root / "tls",
                    selected=selected,
                )
            elif group_key == KNOWN_HOSTS_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(layout_paths["known_hosts_path"]),
                    selected=selected,
                )
            else:
                continue
            bundle_groups.append(group)
            bundle_members.extend(members)

        return bundle_groups, bundle_members

    def _collect_debug_bundle(
        self,
        app_settings: Settings,
        history_snapshot_path: Path | None,
        *,
        selected_groups: list[str],
        scrubber: DebugScrubber | None,
        runtime_payload: dict[str, Any] | None,
        maintenance_payload: dict[str, Any] | None,
        exported_at: datetime,
    ) -> tuple[list[BundleGroup], list[BundleMember]]:
        layout_paths = _derive_runtime_layout_paths(app_settings.config_file)
        config_root = Path(app_settings.config_file).parent
        bundle_groups: list[BundleGroup] = []
        bundle_members: list[BundleMember] = []

        for group_key in BACKUP_GROUP_METADATA:
            metadata = BACKUP_GROUP_METADATA[group_key]
            if "debug" not in metadata["bundle_types"]:
                continue
            selected = group_key in selected_groups
            if group_key == CONFIG_FILE_KEY:
                content_bytes = self._read_scrubbed_yaml_file(Path(app_settings.config_file), scrubber)
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=app_settings.config_file,
                )
            elif group_key == RUNTIME_OVERRIDES_FILE_KEY:
                content_bytes = self._read_scrubbed_yaml_file(Path(app_settings.paths.runtime_overrides_file), scrubber)
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=app_settings.paths.runtime_overrides_file,
                )
            elif group_key == PROFILE_FILE_KEY:
                content_bytes = self._read_scrubbed_yaml_file(Path(app_settings.paths.profile_file), scrubber)
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=app_settings.paths.profile_file,
                )
            elif group_key == MAPPING_FILE_KEY:
                content_bytes = self._read_scrubbed_json_file(Path(app_settings.paths.mapping_file), scrubber)
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=app_settings.paths.mapping_file,
                )
            elif group_key == SAS_FABRIC_ALIAS_FILE_KEY:
                content_bytes = self._read_scrubbed_json_file(Path(app_settings.paths.sas_fabric_alias_file), scrubber)
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=app_settings.paths.sas_fabric_alias_file,
                )
            elif group_key == SLOT_DETAIL_FILE_KEY:
                content_bytes = self._read_scrubbed_json_file(Path(app_settings.paths.slot_detail_cache_file), scrubber)
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=app_settings.paths.slot_detail_cache_file,
                )
            elif group_key == HISTORY_DB_KEY:
                group, members = self._collect_generated_path_group(
                    group_key,
                    history_snapshot_path,
                    selected=selected,
                    source_path=str(self.store.file_path),
                )
            elif group_key == SSH_KEYS_KEY:
                group, members = self._collect_directory_group(
                    group_key,
                    config_root / "ssh",
                    selected=selected,
                )
            elif group_key == TLS_TRUST_KEY:
                group, members = self._collect_directory_group(
                    group_key,
                    config_root / "tls",
                    selected=selected,
                )
            elif group_key == KNOWN_HOSTS_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(layout_paths["known_hosts_path"]),
                    selected=selected,
                )
            elif group_key == DEBUG_STATE_KEY:
                content_bytes = self._build_debug_state_bytes(
                    app_settings,
                    runtime_payload=runtime_payload,
                    maintenance_payload=maintenance_payload,
                    selected_groups=selected_groups,
                    scrubber=scrubber,
                    exported_at=exported_at,
                )
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=None,
                )
            elif group_key == DEBUG_README_KEY:
                content_bytes = self._build_debug_readme_bytes(
                    scrub_secrets=bool(scrubber and scrubber.scrub_secrets),
                    scrub_disk_identifiers=bool(scrubber and scrubber.scrub_disk_identifiers),
                )
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=None,
                )
            else:
                continue
            bundle_groups.append(group)
            bundle_members.extend(members)

        return bundle_groups, bundle_members

    def _collect_file_group(
        self,
        group_key: str,
        source_path: Path,
        *,
        selected: bool,
    ) -> tuple[BundleGroup, list[BundleMember]]:
        metadata = BACKUP_GROUP_METADATA[group_key]
        if not selected:
            return (
                BundleGroup(
                    key=group_key,
                    label=metadata["label"],
                    archive_root=metadata["archive_root"],
                    source_path=str(source_path),
                    selected=False,
                    present=False,
                    sensitive=bool(metadata["sensitive"]),
                    restore_mode=metadata["restore_mode"],
                ),
                [],
            )

        if source_path.exists() and source_path.is_file():
            member = BundleMember(
                key=group_key,
                group_key=group_key,
                archive_path=metadata["archive_root"],
                source_path=str(source_path),
                present=True,
                content=source_path.read_bytes(),
            )
            return (
                BundleGroup(
                    key=group_key,
                    label=metadata["label"],
                    archive_root=metadata["archive_root"],
                    source_path=str(source_path),
                    selected=True,
                    present=True,
                    sensitive=bool(metadata["sensitive"]),
                    restore_mode=metadata["restore_mode"],
                ),
                [member],
            )

        return (
            BundleGroup(
                key=group_key,
                label=metadata["label"],
                archive_root=metadata["archive_root"],
                source_path=str(source_path),
                selected=True,
                present=False,
                sensitive=bool(metadata["sensitive"]),
                restore_mode=metadata["restore_mode"],
            ),
            [],
        )

    def _collect_generated_file_group(
        self,
        group_key: str,
        content_bytes: bytes,
        *,
        selected: bool,
        source_path: str | None,
    ) -> tuple[BundleGroup, list[BundleMember]]:
        metadata = BACKUP_GROUP_METADATA[group_key]
        if not selected:
            return (
                BundleGroup(
                    key=group_key,
                    label=metadata["label"],
                    archive_root=metadata["archive_root"],
                    source_path=source_path,
                    selected=False,
                    present=False,
                    sensitive=bool(metadata["sensitive"]),
                    restore_mode=metadata["restore_mode"],
                ),
                [],
            )

        present = bool(content_bytes)
        members = []
        if present:
            members.append(
                BundleMember(
                    key=group_key,
                    group_key=group_key,
                    archive_path=metadata["archive_root"],
                    source_path=source_path,
                    present=True,
                    content=content_bytes,
                )
            )
        return (
            BundleGroup(
                key=group_key,
                label=metadata["label"],
                archive_root=metadata["archive_root"],
                source_path=source_path,
                selected=True,
                present=present,
                sensitive=bool(metadata["sensitive"]),
                restore_mode=metadata["restore_mode"],
            ),
            members,
        )

    def _collect_generated_path_group(
        self,
        group_key: str,
        file_path: Path | None,
        *,
        selected: bool,
        source_path: str | None,
    ) -> tuple[BundleGroup, list[BundleMember]]:
        metadata = BACKUP_GROUP_METADATA[group_key]
        present = bool(
            selected
            and file_path is not None
            and file_path.is_file()
            and file_path.stat().st_size > 0
        )
        members = (
            [
                BundleMember(
                    key=group_key,
                    group_key=group_key,
                    archive_path=metadata["archive_root"],
                    source_path=source_path,
                    present=True,
                    content=None,
                    file_path=file_path,
                )
            ]
            if present
            else []
        )
        return (
            BundleGroup(
                key=group_key,
                label=metadata["label"],
                archive_root=metadata["archive_root"],
                source_path=source_path,
                selected=selected,
                present=present,
                sensitive=bool(metadata["sensitive"]),
                restore_mode=metadata["restore_mode"],
            ),
            members,
        )

    def _collect_directory_group(
        self,
        group_key: str,
        source_dir: Path,
        *,
        selected: bool,
    ) -> tuple[BundleGroup, list[BundleMember]]:
        metadata = BACKUP_GROUP_METADATA[group_key]
        if not selected:
            return (
                BundleGroup(
                    key=group_key,
                    label=metadata["label"],
                    archive_root=metadata["archive_root"],
                    source_path=str(source_dir),
                    selected=False,
                    present=False,
                    sensitive=bool(metadata["sensitive"]),
                    restore_mode=metadata["restore_mode"],
                ),
                [],
            )

        members: list[BundleMember] = []
        if source_dir.exists() and source_dir.is_dir():
            for file_path in sorted(path for path in source_dir.rglob("*") if path.is_file()):
                relative_path = file_path.relative_to(source_dir).as_posix()
                member_key = f"{group_key}:{relative_path}"
                archive_path = f"{metadata['archive_root']}/{relative_path}"
                members.append(
                    BundleMember(
                        key=member_key,
                        group_key=group_key,
                        archive_path=archive_path,
                        source_path=str(file_path),
                        present=True,
                        content=file_path.read_bytes(),
                    )
                )

        return (
            BundleGroup(
                key=group_key,
                label=metadata["label"],
                archive_root=metadata["archive_root"],
                source_path=str(source_dir),
                selected=True,
                present=bool(members),
                sensitive=bool(metadata["sensitive"]),
                restore_mode=metadata["restore_mode"],
            ),
            members,
        )

    def _build_manifest(
        self,
        *,
        format_name: str,
        app_settings: Settings,
        exported_at: datetime,
        packaging: ArchivePackaging,
        bundle_groups: list[BundleGroup],
        bundle_members: list[BundleMember],
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        member_limit, expanded_limit = self._expanded_limits_for_packaging(packaging)
        large_member_group_keys = (
            frozenset({HISTORY_DB_KEY}) if packaging == "7z" else frozenset()
        )
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": format_name,
            "app_version": __version__,
            "exported_at": exported_at.isoformat(),
            "packaging": packaging,
            "default_system_id": app_settings.default_system_id,
            "systems": [
                {
                    "id": system.id,
                    "label": system.label,
                    "platform": system.truenas.platform,
                }
                for system in app_settings.systems
            ],
            "groups": self._collect_group_specs(bundle_groups),
            "files": self._collect_file_specs(
                bundle_members,
                member_limit=member_limit,
                expanded_limit=expanded_limit,
                large_member_group_keys=large_member_group_keys,
            ),
        }
        if extra_fields:
            manifest.update(extra_fields)
        return manifest

    @staticmethod
    def _collect_group_specs(bundle_groups: list[BundleGroup]) -> list[dict[str, Any]]:
        return [
            {
                "key": group.key,
                "label": group.label,
                "archive_root": group.archive_root,
                "source_path": group.source_path,
                "selected": group.selected,
                "present": group.present,
                "sensitive": group.sensitive,
                "restore_mode": group.restore_mode,
            }
            for group in bundle_groups
        ]

    @classmethod
    def _collect_file_specs(
        cls,
        bundle_members: list[BundleMember],
        *,
        member_limit: int = MAX_ARCHIVE_MEMBER_BYTES,
        expanded_limit: int = MAX_ARCHIVE_EXPANDED_BYTES,
        large_member_group_keys: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        if len(bundle_members) + 1 > MAX_ARCHIVE_MEMBER_COUNT:
            raise ValueError("Backup bundle archive contains too many members.")
        manifest_files: list[dict[str, Any]] = []
        expanded_total = 0
        for member in bundle_members:
            size_bytes, digest = cls._bundle_member_size_and_digest(member)
            effective_member_limit = (
                member_limit
                if member.group_key in large_member_group_keys
                else min(member_limit, MAX_ARCHIVE_MEMBER_BYTES)
            )
            if size_bytes > effective_member_limit:
                raise ValueError("Backup bundle archive member exceeds its expanded byte limit.")
            expanded_total += size_bytes
            if expanded_total > expanded_limit:
                raise ValueError("Backup bundle archive expanded data exceeds its byte limit.")
            manifest_files.append(
                {
                    "key": member.key,
                    "group_key": member.group_key,
                    "archive_path": member.archive_path,
                    "source_path": member.source_path,
                    "size_bytes": size_bytes,
                    "sha256": digest,
                }
            )
        return manifest_files

    @staticmethod
    def _expanded_limits_for_packaging(packaging: ArchivePackaging) -> tuple[int, int]:
        if packaging == "7z":
            return (
                MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
            )
        return MAX_ARCHIVE_MEMBER_BYTES, MAX_ARCHIVE_EXPANDED_BYTES

    def _validate_export_archive(
        self,
        archive_path: Path,
        packaging: ArchivePackaging,
        *,
        expanded_archive_size: int | None,
        passphrase: str | None,
    ) -> None:
        archive_size = archive_path.stat().st_size
        archive_limit = (
            MAX_FILE_BACKED_BACKUP_ARCHIVE_BYTES
            if packaging == "7z"
            else MAX_BACKUP_ARCHIVE_BYTES
        )
        if archive_size > archive_limit:
            raise ValueError(
                f"Backup bundle archive exceeds the {archive_limit}-byte input limit."
            )
        if packaging == "zip":
            try:
                with zipfile.ZipFile(archive_path, mode="r") as archive:
                    members = archive.infolist()
            except zipfile.BadZipFile as exc:
                raise ValueError("Backup bundle ZIP archive is corrupted.") from exc
            if len(members) > MAX_ARCHIVE_MEMBER_COUNT:
                raise ValueError("Backup bundle archive contains too many members.")
            self._validate_declared_archive_limits(
                [(member.compress_size, member.file_size) for member in members]
            )
            return
        if packaging in {"tar.gz", "tar.zst"}:
            if expanded_archive_size is None:
                raise ValueError("Backup bundle TAR archive size could not be verified.")
            self._validate_declared_archive_limits(
                [(archive_size, expanded_archive_size)],
                compressed_total=archive_size,
            )
            return
        if packaging == "7z":
            command = [
                "l",
                "-slt",
                str(archive_path),
            ]
            prompt_passphrase = passphrase if passphrase else None
            if prompt_passphrase is None:
                command.append("-p")
            list_result = self._run_7z_command(
                command,
                passphrase=prompt_passphrase,
            )
            self._raise_for_7z_failure(
                list_result,
                "Portable 7z backup export could not be verified.",
                passphrase=passphrase,
                reading_archive=True,
            )
            listed_entries = self._seven_zip_listed_entries(
                list_result.stdout,
                archive_path,
            )
            self._validate_7z_listed_entries(
                listed_entries,
                archive_size=archive_size,
                member_limit=MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                expanded_limit=MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
            )
            return
        raise ValueError(f"Unsupported backup packaging '{packaging}'.")

    @staticmethod
    def _bundle_member_size_and_digest(member: BundleMember) -> tuple[int, str | None]:
        if not member.present:
            return 0, None
        if member.file_path is not None:
            digest = hashlib.sha256()
            size_bytes = 0
            with member.file_path.open("rb") as source:
                while chunk := source.read(ARCHIVE_READ_CHUNK_BYTES):
                    size_bytes += len(chunk)
                    digest.update(chunk)
            return size_bytes, digest.hexdigest()
        content = member.content or b""
        return len(content), hashlib.sha256(content).hexdigest()

    def _build_archive(
        self,
        bundle_members: list[BundleMember],
        manifest: dict[str, Any],
        packaging: ArchivePackaging,
        *,
        passphrase: str | None = None,
    ) -> bytes:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / f"bundle{ARCHIVE_FILE_SUFFIXES[packaging]}"
            self._build_archive_to_path(
                bundle_members,
                manifest,
                packaging,
                archive_path,
                passphrase=passphrase,
            )
            return archive_path.read_bytes()

    def _build_archive_to_path(
        self,
        bundle_members: list[BundleMember],
        manifest: dict[str, Any],
        packaging: ArchivePackaging,
        output_path: Path,
        *,
        passphrase: str | None = None,
    ) -> int | None:
        if passphrase is not None and not passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        if packaging == "zip":
            with zipfile.ZipFile(
                output_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                archive.writestr("manifest.json", manifest_bytes)
                for member in bundle_members:
                    self._write_zip_bundle_member(archive, member)
            return None
        if packaging == "7z":
            self._build_7z_archive_to_path(
                bundle_members,
                manifest_bytes,
                output_path,
                passphrase=passphrase,
            )
            return None
        if packaging in {"tar.gz", "tar.zst"}:
            if packaging == "tar.zst" and zstd is None:
                raise ValueError("tar.zst export requires the optional 'zstandard' dependency.")
            tar_path = output_path.with_name(f".{output_path.name}.tar.tmp")
            try:
                with tarfile.open(tar_path, mode="w") as archive:
                    self._write_tar_bundle(archive, bundle_members, manifest_bytes)
                self._validate_export_tar_physical_members(tar_path)
                tar_size = tar_path.stat().st_size
                if packaging == "tar.gz":
                    with tar_path.open("rb") as source, output_path.open("wb") as output:
                        with gzip.GzipFile(
                            fileobj=output,
                            mode="wb",
                            compresslevel=9,
                            mtime=0,
                        ) as compressed:
                            shutil.copyfileobj(
                                source,
                                compressed,
                                length=ARCHIVE_READ_CHUNK_BYTES,
                            )
                else:
                    assert zstd is not None
                    with tar_path.open("rb") as source, output_path.open("wb") as output:
                        with zstd.ZstdCompressor(level=9).stream_writer(
                            output,
                            size=tar_size,
                            closefd=False,
                        ) as compressed:
                            shutil.copyfileobj(
                                source,
                                compressed,
                                length=ARCHIVE_READ_CHUNK_BYTES,
                            )
            finally:
                tar_path.unlink(missing_ok=True)
            return tar_size
        raise ValueError(f"Unsupported backup packaging '{packaging}'.")

    @classmethod
    def _validate_export_tar_physical_members(cls, tar_path: Path) -> None:
        archive_size = tar_path.stat().st_size
        if not archive_size or archive_size % 512:
            raise ValueError("Backup bundle TAR archive framing is invalid.")
        physical_count = 0
        cursor = 0
        with tar_path.open("rb", buffering=0) as archive:
            while cursor + 512 <= archive_size:
                archive.seek(cursor)
                header = archive.read(512)
                if len(header) != 512:
                    raise ValueError("Backup bundle TAR archive framing is invalid.")
                if header == b"\0" * 512:
                    trailer = archive.read(512)
                    if trailer != b"\0" * 512:
                        raise ValueError("Backup bundle TAR archive trailer is truncated.")
                    return
                physical_count += 1
                if physical_count > MAX_ARCHIVE_MEMBER_COUNT:
                    raise ValueError("Backup bundle archive contains too many members.")
                member_size = cls._parse_tar_octal(header[124:136], label="member size")
                padded_size = ((member_size + 511) // 512) * 512
                cursor += 512 + padded_size
                if cursor > archive_size:
                    raise ValueError("Backup bundle TAR member data is truncated.")
        raise ValueError("Backup bundle TAR archive is missing its trailer.")

    @classmethod
    def _write_zip_bundle_member(
        cls,
        archive: zipfile.ZipFile,
        member: BundleMember,
    ) -> None:
        if not member.present:
            return
        if member.file_path is None:
            archive.writestr(member.archive_path, member.content or b"")
            return
        with member.file_path.open("rb") as source, archive.open(member.archive_path, mode="w") as target:
            shutil.copyfileobj(source, target, length=ARCHIVE_READ_CHUNK_BYTES)

    @classmethod
    def _write_tar_bundle(
        cls,
        archive: tarfile.TarFile,
        bundle_members: list[BundleMember],
        manifest_bytes: bytes,
    ) -> None:
        cls._add_tar_member(archive, "manifest.json", manifest_bytes)
        for member in bundle_members:
            if not member.present:
                continue
            if member.file_path is None:
                cls._add_tar_member(archive, member.archive_path, member.content or b"")
                continue
            info = tarfile.TarInfo(name=member.archive_path)
            info.size = member.file_path.stat().st_size
            with member.file_path.open("rb") as source:
                archive.addfile(info, source)

    def _build_7z_archive_to_path(
        self,
        bundle_members: list[BundleMember],
        manifest_bytes: bytes,
        archive_path: Path,
        *,
        passphrase: str | None = None,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            staging_dir = Path(temp_dir) / "bundle"
            staging_dir.mkdir(parents=True, exist_ok=True)
            (staging_dir / "manifest.json").write_bytes(manifest_bytes)
            top_level_entries = {"manifest.json"}
            for member in bundle_members:
                if not member.present:
                    continue
                target_path = staging_dir / member.archive_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if member.file_path is not None:
                    shutil.copyfile(member.file_path, target_path)
                else:
                    target_path.write_bytes(member.content or b"")
                top_level_entries.add(member.archive_path.split("/", 1)[0])
            command = [
                "a",
                "-t7z",
                "-y",
                "-bd",
                "-mx=5",
                "-m0=lzma2",
                "-mmt=1",
                str(archive_path),
                *sorted(top_level_entries),
            ]
            if passphrase is not None:
                command.insert(4, "-mhe=on")
                command.insert(4, "-p")
            result = self._run_7z_command(
                command,
                cwd=staging_dir,
                passphrase=passphrase,
            )
            self._raise_for_7z_failure(
                result,
                "Portable 7z backup export failed.",
                passphrase=passphrase,
            )

    def _read_archive(
        self,
        archive_bytes: bytes,
        *,
        passphrase: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, ExtractedMember], ArchivePackaging, dict[str, Any]]:
        if archive_bytes.startswith(ENCRYPTED_BACKUP_MAGIC):
            decrypted = self._decrypt_scheduled_archive(archive_bytes, passphrase)
            manifest, extracted, packaging, archive_meta = self._read_archive(decrypted)
            return manifest, extracted, packaging, {
                **archive_meta,
                "encrypted": True,
                "encryption": "aes-256-gcm-scrypt",
            }

        packaging = self._detect_archive_packaging(archive_bytes)
        if not packaging:
            raise ValueError("Backup bundle archive format is not supported.")

        if packaging == "7z":
            return self._read_7z_archive(archive_bytes, passphrase=passphrase)

        if packaging == "zip":
            self._preflight_zip_archive(archive_bytes)
            try:
                with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
                    members = archive.infolist()
                    self._validate_zip_members(members)
                    physical_paths = [member.filename for member in members if not member.is_dir()]
                    self._validate_unique_physical_archive_paths(
                        physical_paths
                    )
                    try:
                        manifest_member = archive.getinfo("manifest.json")
                    except KeyError as exc:
                        raise ValueError("Backup bundle is missing manifest.json.") from exc
                    if manifest_member.file_size > MAX_MANIFEST_BYTES:
                        raise ValueError("Backup bundle manifest exceeds its size limit.")
                    manifest_bytes = archive.read(manifest_member)
                    manifest = self._load_manifest(manifest_bytes)
                    self._validate_manifest_before_extraction(manifest)
                    self._validate_restore_schema_support(manifest)
                    self._validate_supported_physical_archive_paths(physical_paths, manifest)
                    extracted = self._extract_manifest_zip_members(archive, manifest)
            except zipfile.BadZipFile as exc:
                raise ValueError("Backup bundle ZIP archive is corrupted.") from exc
            return manifest, extracted, packaging, {"encrypted": False}

        tar_bytes = self._decompress_tar_archive(archive_bytes, packaging)
        preflight_tar_paths = self._preflight_tar_archive(tar_bytes)
        try:
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
                members = self._read_bounded_tar_members(archive)
                physical_paths = [member.name for member in members if member.isfile()]
                if physical_paths != preflight_tar_paths:
                    raise ValueError(
                        "Backup bundle TAR members do not match its physical headers."
                    )
                self._validate_unique_physical_archive_paths(
                    physical_paths
                )
                manifest = self._load_manifest(self._read_tar_member(archive, "manifest.json"))
                self._validate_manifest_before_extraction(manifest)
                self._validate_restore_schema_support(manifest)
                self._validate_supported_physical_archive_paths(physical_paths, manifest)
                extracted = self._extract_manifest_tar_members(archive, manifest)
        except tarfile.TarError as exc:
            raise ValueError("Backup bundle TAR archive is corrupted.") from exc
        return manifest, extracted, packaging, {"encrypted": False}

    def _read_archive_file(
        self,
        archive_path: Path,
        *,
        passphrase: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, ExtractedMember], ArchivePackaging, dict[str, Any]]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(archive_path, flags)
        except OSError as exc:
            raise ValueError("Backup bundle archive could not be opened.") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > MAX_FILE_BACKED_BACKUP_ARCHIVE_BYTES
            ):
                raise ValueError("Backup bundle archive exceeds its size or type limits.")
            prefix = os.pread(descriptor, len(SEVEN_ZIP_SIGNATURE), 0)
            if prefix.startswith(SEVEN_ZIP_SIGNATURE):
                workspace = Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-file-import-"))
                private_archive = workspace / "bundle.7z"
                try:
                    with os.fdopen(os.dup(descriptor), "rb", closefd=True) as source, private_archive.open(
                        "xb"
                    ) as destination:
                        source.seek(0)
                        shutil.copyfileobj(source, destination, length=ARCHIVE_READ_CHUNK_BYTES)
                        destination.flush()
                        os.fsync(destination.fileno())
                    private_archive.chmod(0o600)
                    return self._read_7z_archive(
                        archive_path=private_archive,
                        passphrase=passphrase,
                        workspace=workspace,
                    )
                except Exception:
                    shutil.rmtree(workspace, ignore_errors=True)
                    raise
            if metadata.st_size > MAX_BACKUP_ARCHIVE_BYTES:
                raise ValueError(
                    "Large file-backed backup imports require portable 7z packaging."
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(ARCHIVE_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("Backup bundle archive is truncated.")
                chunks.append(chunk)
                remaining -= len(chunk)
            return self._read_archive(b"".join(chunks), passphrase=passphrase)
        finally:
            os.close(descriptor)

    def _read_7z_archive(
        self,
        archive_bytes: bytes | None = None,
        *,
        archive_path: Path | None = None,
        passphrase: str | None = None,
        workspace: Path | None = None,
    ) -> tuple[dict[str, Any], dict[str, ExtractedMember], ArchivePackaging, dict[str, Any]]:
        if (archive_bytes is None) == (archive_path is None):
            raise ValueError("Exactly one 7z archive source is required.")
        temp_root = (
            Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-7z-import-"))
            if workspace is None
            else workspace
        )
        try:
            if archive_path is None:
                archive_path = temp_root / "bundle.7z"
                archive_path.write_bytes(archive_bytes or b"")
                archive_size = len(archive_bytes or b"")
            else:
                archive_path = Path(archive_path).absolute()
                archive_size = archive_path.stat(follow_symlinks=False).st_size
            extract_dir = temp_root / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)

            prompt_passphrase = passphrase if passphrase else None
            list_command = [
                "l",
                "-slt",
                str(archive_path),
            ]
            if prompt_passphrase is None:
                list_command.append("-p")
            list_result = self._run_7z_command(
                list_command,
                passphrase=prompt_passphrase,
            )
            self._raise_for_7z_failure(
                list_result,
                "Backup bundle 7z archive could not be listed.",
                passphrase=passphrase,
                reading_archive=True,
            )
            listed_entries = self._seven_zip_listed_entries(list_result.stdout, archive_path)
            self._validate_7z_listed_entries(
                listed_entries,
                archive_size=archive_size,
                member_limit=MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                expanded_limit=MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
            )
            listed_paths = [entry["Path"] for entry in listed_entries]
            listed_file_paths = [
                entry["Path"]
                for entry in listed_entries
                if not self._is_7z_directory_entry(entry)
            ]
            self._validate_unique_physical_archive_paths(
                listed_paths
            )
            encrypted = "Encrypted = +" in list_result.stdout or "7zAES" in list_result.stdout

            manifest_entries = [
                entry
                for entry in listed_entries
                if not self._is_7z_directory_entry(entry)
                and self._normalize_archive_member_path(entry["Path"]) == "manifest.json"
            ]
            if len(manifest_entries) != 1:
                raise ValueError("Backup bundle is missing manifest.json.")
            manifest_entry = manifest_entries[0]
            if int(manifest_entry["Size"]) > MAX_MANIFEST_BYTES:
                raise ValueError("Backup bundle manifest exceeds its size limit.")

            manifest_extract_command = [
                "x",
                str(archive_path),
                f"-o{extract_dir}",
                "-y",
                "-bd",
            ]
            if prompt_passphrase is None:
                manifest_extract_command.append("-p")
            manifest_extract_command.append("manifest.json")
            manifest_extract_result = self._run_7z_command(
                manifest_extract_command,
                passphrase=prompt_passphrase,
            )
            self._raise_for_7z_failure(
                manifest_extract_result,
                "Backup bundle 7z manifest could not be extracted.",
                passphrase=passphrase,
                reading_archive=True,
            )

            manifest_path = extract_dir / "manifest.json"
            if not manifest_path.exists():
                raise ValueError("Backup bundle is missing manifest.json.")
            self._validate_extracted_7z_tree(extract_dir, [manifest_entry])
            if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("Backup bundle manifest exceeds its size limit.")
            manifest = self._load_manifest(manifest_path.read_bytes())
            self._validate_manifest_before_extraction(
                manifest,
                member_limit=MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                expanded_limit=MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
                large_member_group_keys=frozenset({HISTORY_DB_KEY}),
            )
            self._validate_restore_schema_support(manifest)
            self._validate_supported_physical_archive_paths(listed_file_paths, manifest)
            self._validate_7z_listing_against_manifest(listed_entries, manifest)

            extract_command = [
                "x",
                str(archive_path),
                f"-o{extract_dir}",
                "-y",
                "-bd",
            ]
            if prompt_passphrase is None:
                extract_command.append("-p")
            extract_result = self._run_7z_command(
                extract_command,
                passphrase=prompt_passphrase,
            )
            self._raise_for_7z_failure(
                extract_result,
                "Backup bundle 7z archive could not be extracted.",
                passphrase=passphrase,
                reading_archive=True,
            )
            self._validate_extracted_7z_tree(extract_dir, listed_entries)
            extracted = self._extract_manifest_directory_members(extract_dir, manifest)
            return manifest, extracted, "7z", {
                "encrypted": encrypted,
                "_cleanup_root": temp_root,
            }
        except Exception:
            try:
                shutil.rmtree(temp_root)
            except Exception as cleanup_error:
                raise RuntimeError(
                    "Backup bundle 7z workspace cleanup failed."
                ) from cleanup_error
            raise

    @staticmethod
    def _load_manifest(manifest_bytes: bytes) -> dict[str, Any]:
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("Backup bundle manifest exceeds its size limit.")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            payload: dict[str, Any] = {}
            for key, value in pairs:
                if key in payload:
                    raise ValueError("Backup bundle manifest contains a duplicate JSON key.")
                payload[key] = value
            return payload

        try:
            manifest = json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Backup bundle manifest is not valid JSON.") from exc
        if not isinstance(manifest, dict):
            raise ValueError("Backup bundle manifest is not valid JSON.")
        return manifest

    @staticmethod
    def _validate_restore_schema_support(manifest: dict[str, Any]) -> None:
        if manifest.get("schema_version") not in SUPPORTED_BUNDLE_SCHEMA_VERSIONS:
            raise ValueError("Backup bundle schema is not supported for restore.")

    @classmethod
    def _is_declared_history_manifest_member(
        cls,
        manifest: dict[str, Any],
        *,
        key: str,
        group_key: str,
        archive_path: str,
    ) -> bool:
        history_metadata = BACKUP_GROUP_METADATA[HISTORY_DB_KEY]
        if (
            key != HISTORY_DB_KEY
            or group_key != HISTORY_DB_KEY
            or archive_path != history_metadata["archive_root"]
        ):
            return False
        history_groups = [
            group
            for group in manifest.get("groups", [])
            if isinstance(group, dict) and group.get("key") == HISTORY_DB_KEY
        ]
        if len(history_groups) != 1:
            return False
        history_group = history_groups[0]
        return (
            history_group.get("archive_root") == history_metadata["archive_root"]
            and history_group.get("selected") is True
            and history_group.get("present") is True
            and history_group.get("restore_mode") == history_metadata["restore_mode"]
        )

    @classmethod
    def _validate_manifest_before_extraction(
        cls,
        manifest: dict[str, Any],
        *,
        member_limit: int = MAX_ARCHIVE_MEMBER_BYTES,
        expanded_limit: int = MAX_ARCHIVE_EXPANDED_BYTES,
        large_member_group_keys: frozenset[str] = frozenset(),
    ) -> None:
        schema_version = manifest.get("schema_version")
        if (
            type(schema_version) is not int
            or schema_version not in SUPPORTED_BUNDLE_SCHEMA_VERSIONS
        ):
            raise ValueError(f"Unsupported backup schema version {schema_version!r}.")
        if manifest.get("format") != BUNDLE_FORMAT:
            raise ValueError("Backup bundle format is not recognized.")
        if schema_version == SEGMENTED_BACKUP_SCHEMA_VERSION:
            validate_segmented_manifest(manifest)

        for collection_name in ("groups", "files"):
            collection = manifest.get(collection_name, [])
            if not isinstance(collection, list):
                raise ValueError(f"Backup bundle manifest {collection_name} must be a list.")
            if len(collection) > MAX_ARCHIVE_MEMBER_COUNT:
                raise ValueError(
                    f"Backup bundle manifest {collection_name} exceeds its entry limit."
                )
            if any(not isinstance(entry, dict) for entry in collection):
                raise ValueError(
                    f"Backup bundle manifest {collection_name} entries must be objects."
                )

        seen_group_keys: set[str] = set()
        for raw_group in manifest.get("groups", []):
            group_key = str(raw_group.get("key") or "").strip()
            if group_key and group_key in seen_group_keys:
                raise ValueError(
                    f"Backup bundle manifest contains duplicate group key {group_key!r}."
                )
            if group_key:
                seen_group_keys.add(group_key)

        seen_keys: set[str] = set()
        seen_paths: set[str] = set()
        declared_total = 0
        for index, raw_entry in enumerate(manifest.get("files", [])):
            raw_archive_path = str(raw_entry.get("archive_path") or "").strip()
            if not raw_archive_path:
                raise ValueError("Backup bundle manifest member is missing its archive path.")
            archive_path = cls._normalize_archive_member_path(raw_archive_path)
            if archive_path == "manifest.json":
                raise ValueError("Backup bundle manifest cannot declare manifest.json as a member.")
            key = str(raw_entry.get("key") or archive_path or f"member-{index}").strip()
            if key in seen_keys:
                raise ValueError(f"Backup bundle manifest contains duplicate member key {key!r}.")
            if archive_path in seen_paths:
                raise ValueError(
                    f"Backup bundle manifest contains duplicate archive path {archive_path!r}."
                )
            seen_keys.add(key)
            seen_paths.add(archive_path)

            declared_history_member = cls._is_declared_history_manifest_member(
                manifest,
                key=key,
                group_key=str(raw_entry.get("group_key") or "").strip(),
                archive_path=archive_path,
            )
            if raw_entry.get("group_key") == HISTORY_DB_KEY and not declared_history_member:
                raise ValueError(
                    "Backup bundle history member does not match its declared backup group."
                )

            expected_size = raw_entry.get("size_bytes")
            if expected_size is not None:
                if (
                    isinstance(expected_size, bool)
                    or not isinstance(expected_size, int)
                    or expected_size < 0
                ):
                    raise ValueError(
                        f"Backup bundle member {archive_path} has invalid size metadata."
                    )
                effective_member_limit = (
                    member_limit
                    if (
                        declared_history_member
                        and HISTORY_DB_KEY in large_member_group_keys
                    )
                    else min(member_limit, MAX_ARCHIVE_MEMBER_BYTES)
                )
                if expected_size > effective_member_limit:
                    raise ValueError(
                        f"Backup bundle member {archive_path} exceeds its expanded byte limit."
                    )
                declared_total += expected_size
                if declared_total > expanded_limit:
                    raise ValueError(
                        "Backup bundle manifest members exceed the expanded byte limit."
                    )

            expected_sha256 = raw_entry.get("sha256")
            if expected_sha256 is not None:
                digest = str(expected_sha256).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError(
                        f"Backup bundle member {archive_path} has invalid SHA-256 metadata."
                    )

    @staticmethod
    def _validate_declared_archive_limits(
        entries: list[tuple[int, int]],
        *,
        compressed_total: int | None = None,
        member_limit: int = MAX_ARCHIVE_MEMBER_BYTES,
        expanded_limit: int = MAX_ARCHIVE_EXPANDED_BYTES,
    ) -> None:
        if len(entries) > MAX_ARCHIVE_MEMBER_COUNT:
            raise ValueError("Backup bundle archive contains too many members.")
        expanded_total = 0
        declared_compressed_total = 0
        for compressed_size, expanded_size in entries:
            if compressed_size < 0 or expanded_size < 0:
                raise ValueError("Backup bundle archive contains invalid member size metadata.")
            if expanded_size > member_limit:
                raise ValueError("Backup bundle archive member exceeds its expanded byte limit.")
            if (
                compressed_total is None
                and expanded_size > MAX_ARCHIVE_COMPRESSION_RATIO * max(compressed_size, 1)
            ):
                raise ValueError("Backup bundle archive member compression ratio exceeds its limit.")
            expanded_total += expanded_size
            declared_compressed_total += compressed_size
            if expanded_total > expanded_limit:
                raise ValueError("Backup bundle archive expanded data exceeds its byte limit.")
        ratio_denominator = (
            declared_compressed_total
            if compressed_total is None
            else compressed_total
        )
        if expanded_total > MAX_ARCHIVE_COMPRESSION_RATIO * max(ratio_denominator, 1):
            raise ValueError("Backup bundle archive compression ratio exceeds its limit.")

    @staticmethod
    def _validate_zip_extra_metadata(extra: bytes) -> None:
        extra_cursor = 0
        while extra_cursor < len(extra):
            if extra_cursor + 4 > len(extra):
                raise ValueError("Backup bundle ZIP archive extra metadata is malformed.")
            extra_id, extra_length = struct.unpack_from("<HH", extra, extra_cursor)
            extra_cursor += 4
            if extra_cursor + extra_length > len(extra):
                raise ValueError("Backup bundle ZIP archive extra metadata is malformed.")
            if extra_id == 0x0001:
                raise ValueError("Backup bundle ZIP archive uses unsupported ZIP64 metadata.")
            extra_cursor += extra_length

    @classmethod
    def _preflight_zip_archive(cls, archive_bytes: bytes) -> None:
        eocd_signature = b"PK\x05\x06"
        search_start = max(0, len(archive_bytes) - (65535 + 22))
        candidates: list[tuple[int, tuple[Any, ...]]] = []
        signature_positions: list[int] = []
        position = archive_bytes.find(eocd_signature, search_start)
        while position >= 0:
            signature_positions.append(position)
            if position + 22 <= len(archive_bytes):
                fields = struct.unpack_from("<4s4H2IH", archive_bytes, position)
                if position + 22 + fields[7] == len(archive_bytes):
                    candidates.append((position, fields))
            position = archive_bytes.find(eocd_signature, position + 1)
        if (
            len(candidates) != 1
            or not signature_positions
            or candidates[0][0] != signature_positions[-1]
        ):
            raise ValueError("Backup bundle ZIP archive has an invalid or ambiguous directory footer.")

        eocd_offset, fields = candidates[0]
        disk_number, directory_disk, disk_entries, total_entries = fields[1:5]
        directory_size, directory_offset = fields[5:7]
        if (
            disk_number != 0
            or directory_disk != 0
            or disk_entries != total_entries
            or total_entries == 0xFFFF
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        ):
            raise ValueError("Backup bundle ZIP archive uses unsupported multidisk or ZIP64 metadata.")
        if total_entries > MAX_ARCHIVE_MEMBER_COUNT:
            raise ValueError("Backup bundle archive contains too many members.")
        if directory_size > MAX_ARCHIVE_METADATA_BYTES:
            raise ValueError("Backup bundle ZIP directory metadata exceeds its byte limit.")
        if directory_offset + directory_size != eocd_offset:
            raise ValueError("Backup bundle ZIP archive directory offsets are invalid.")

        directory_end = directory_offset + directory_size
        cursor = directory_offset
        declared_entries: list[tuple[int, int]] = []
        local_ranges: list[tuple[int, int]] = []
        parsed_count = 0
        while cursor < directory_end:
            if cursor + 46 > directory_end:
                raise ValueError("Backup bundle ZIP archive directory is truncated.")
            central = struct.unpack_from("<4s6H3I5H2I", archive_bytes, cursor)
            if central[0] != b"PK\x01\x02":
                raise ValueError("Backup bundle ZIP archive directory is malformed.")
            compressed_size = central[8]
            expanded_size = central[9]
            filename_size, extra_size, comment_size = central[10:13]
            disk_start = central[13]
            local_offset = central[16]
            general_flags = central[3]
            compression_method = central[4]
            if general_flags & (0x0001 | 0x0040 | 0x2000):
                raise ValueError("Backup bundle ZIP archive uses unsupported encryption flags.")
            if general_flags & 0x0008:
                raise ValueError("Backup bundle ZIP archive uses an unsupported data descriptor.")
            if compression_method not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError(
                    "Backup bundle ZIP archive uses an unsupported compression method."
                )
            if (
                compressed_size == 0xFFFFFFFF
                or expanded_size == 0xFFFFFFFF
                or disk_start == 0xFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise ValueError("Backup bundle ZIP archive uses unsupported ZIP64 metadata.")
            record_end = cursor + 46 + filename_size + extra_size + comment_size
            if record_end > directory_end:
                raise ValueError("Backup bundle ZIP archive directory record is truncated.")
            if local_offset + 30 > directory_offset:
                raise ValueError("Backup bundle ZIP archive local header is invalid.")
            local_header = struct.unpack_from("<4s5H3I2H", archive_bytes, local_offset)
            if (
                local_header[0] != b"PK\x03\x04"
                or local_header[1] != central[2]
                or local_header[2] != general_flags
                or local_header[3] != compression_method
                or local_header[4] != central[5]
                or local_header[5] != central[6]
            ):
                raise ValueError("Backup bundle ZIP archive local header is inconsistent.")
            local_crc, local_compressed_size, local_expanded_size = local_header[6:9]
            if (
                local_compressed_size == 0xFFFFFFFF
                or local_expanded_size == 0xFFFFFFFF
            ):
                raise ValueError("Backup bundle ZIP archive uses unsupported ZIP64 metadata.")
            if (
                local_crc != central[7]
                or local_compressed_size != compressed_size
                or local_expanded_size != expanded_size
            ):
                raise ValueError("Backup bundle ZIP archive local header is inconsistent.")
            local_filename_size, local_extra_size = local_header[9:11]
            local_record_end = local_offset + 30 + local_filename_size + local_extra_size
            if local_record_end > directory_offset:
                raise ValueError("Backup bundle ZIP archive local header is truncated.")
            local_data_end = local_record_end + compressed_size
            if local_data_end > directory_offset:
                raise ValueError("Backup bundle ZIP archive member data is truncated.")
            local_ranges.append((local_offset, local_data_end))
            central_filename = archive_bytes[cursor + 46 : cursor + 46 + filename_size]
            local_filename = archive_bytes[local_offset + 30 : local_offset + 30 + local_filename_size]
            if central_filename != local_filename:
                raise ValueError("Backup bundle ZIP archive member names are inconsistent.")
            local_extra = archive_bytes[
                local_offset + 30 + local_filename_size : local_record_end
            ]
            cls._validate_zip_extra_metadata(local_extra)
            extra = archive_bytes[cursor + 46 + filename_size : cursor + 46 + filename_size + extra_size]
            cls._validate_zip_extra_metadata(extra)
            declared_entries.append((compressed_size, expanded_size))
            parsed_count += 1
            if parsed_count > MAX_ARCHIVE_MEMBER_COUNT:
                raise ValueError("Backup bundle archive contains too many members.")
            cursor = record_end
        if cursor != directory_end or parsed_count != total_entries:
            raise ValueError("Backup bundle ZIP archive directory entry count is invalid.")
        for (_, previous_end), (current_start, _) in zip(
            sorted(local_ranges),
            sorted(local_ranges)[1:],
        ):
            if current_start < previous_end:
                raise ValueError("Backup bundle ZIP archive local records overlap.")
        cls._validate_declared_archive_limits(declared_entries)

    @staticmethod
    def _parse_tar_octal(field: bytes, *, label: str) -> int:
        if field and field[0] & 0x80:
            raise ValueError(f"Backup bundle TAR {label} uses unsupported binary metadata.")
        stripped = field.rstrip(b"\0 ").lstrip(b" ")
        if not stripped:
            return 0
        if any(byte not in b"01234567" for byte in stripped):
            raise ValueError(f"Backup bundle TAR {label} is invalid.")
        return int(stripped, 8)

    @classmethod
    def _parse_pax_path(cls, payload: bytes) -> str:
        attributes: dict[str, str] = {}
        cursor = 0
        while cursor < len(payload):
            separator = payload.find(b" ", cursor)
            if separator <= cursor:
                raise ValueError("Backup bundle TAR PAX metadata is malformed.")
            raw_length = payload[cursor:separator]
            if (
                not raw_length.isdigit()
                or len(raw_length) > 20
                or (len(raw_length) > 1 and raw_length.startswith(b"0"))
            ):
                raise ValueError("Backup bundle TAR PAX metadata is malformed.")
            record_length = int(raw_length)
            record_end = cursor + record_length
            if record_length <= separator - cursor + 2 or record_end > len(payload):
                raise ValueError("Backup bundle TAR PAX metadata is malformed.")
            record = payload[separator + 1 : record_end]
            if not record.endswith(b"\n") or b"=" not in record:
                raise ValueError("Backup bundle TAR PAX metadata is malformed.")
            raw_key, raw_value = record[:-1].split(b"=", 1)
            try:
                key = raw_key.decode("ascii")
                value = raw_value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Backup bundle TAR PAX metadata is not valid UTF-8.") from exc
            if key != "path":
                raise ValueError(f"Backup bundle TAR PAX metadata key is unsupported: {key}")
            if key in attributes:
                raise ValueError("Backup bundle TAR PAX metadata contains a duplicate key.")
            attributes[key] = value
            cursor = record_end
        if set(attributes) != {"path"}:
            raise ValueError("Backup bundle TAR PAX metadata must declare exactly one path.")
        return cls._normalize_archive_member_path(attributes["path"])

    @classmethod
    def _preflight_tar_archive(cls, tar_bytes: bytes) -> list[str]:
        if not tar_bytes or len(tar_bytes) % 512:
            raise ValueError("Backup bundle TAR archive framing is invalid.")
        paths: list[str] = []
        expanded_total = 0
        metadata_total = 0
        physical_count = 0
        pending_pax_path: str | None = None
        cursor = 0
        while cursor + 512 <= len(tar_bytes):
            header = tar_bytes[cursor : cursor + 512]
            if header == b"\0" * 512:
                if cursor + 1024 > len(tar_bytes) or tar_bytes[cursor + 512 : cursor + 1024] != b"\0" * 512:
                    raise ValueError("Backup bundle TAR archive trailer is truncated.")
                if any(tar_bytes[cursor + 1024 :]):
                    raise ValueError("Backup bundle TAR archive has non-zero trailing data.")
                if pending_pax_path is not None:
                    raise ValueError("Backup bundle TAR PAX metadata has no following member.")
                if not paths:
                    raise ValueError("Backup bundle TAR archive contains no members.")
                return paths

            if header[257:263] != b"ustar\0" or header[263:265] != b"00":
                raise ValueError("Backup bundle TAR archive is not strict USTAR format.")
            stored_checksum = cls._parse_tar_octal(header[148:156], label="checksum")
            checksum_header = bytearray(header)
            checksum_header[148:156] = b" " * 8
            if sum(checksum_header) != stored_checksum:
                raise ValueError("Backup bundle TAR archive checksum is invalid.")
            member_type = header[156:157]
            if member_type not in {b"0", b"\0", b"x"}:
                raise ValueError("Backup bundle TAR archive contains an unsupported member type.")

            member_size = cls._parse_tar_octal(header[124:136], label="member size")
            if member_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Backup bundle archive member exceeds its expanded byte limit.")
            expanded_total += member_size
            if expanded_total > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ValueError("Backup bundle archive expanded data exceeds its byte limit.")

            physical_count += 1
            if physical_count > MAX_ARCHIVE_MEMBER_COUNT:
                raise ValueError("Backup bundle archive contains too many members.")

            padded_size = ((member_size + 511) // 512) * 512
            member_data_start = cursor + 512
            member_data_end = member_data_start + member_size
            next_cursor = member_data_start + padded_size
            if next_cursor > len(tar_bytes):
                raise ValueError("Backup bundle TAR member data is truncated.")

            if member_type == b"x":
                if pending_pax_path is not None:
                    raise ValueError("Backup bundle TAR archive has stacked PAX metadata.")
                metadata_total += member_size
                if metadata_total > MAX_ARCHIVE_METADATA_BYTES:
                    raise ValueError("Backup bundle TAR metadata exceeds its byte limit.")
                pending_pax_path = cls._parse_pax_path(
                    tar_bytes[member_data_start:member_data_end]
                )
                cursor = next_cursor
                continue

            name_bytes = header[0:100].split(b"\0", 1)[0]
            prefix_bytes = header[345:500].split(b"\0", 1)[0]
            try:
                name = name_bytes.decode("utf-8")
                prefix = prefix_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Backup bundle TAR member name is not valid UTF-8.") from exc
            archive_path = pending_pax_path or (f"{prefix}/{name}" if prefix else name)
            paths.append(cls._normalize_archive_member_path(archive_path))
            pending_pax_path = None
            cursor = next_cursor
        raise ValueError("Backup bundle TAR archive is missing its trailer.")

    @classmethod
    def _validate_zip_members(cls, members: list[zipfile.ZipInfo]) -> None:
        cls._validate_declared_archive_limits(
            [(member.compress_size, member.file_size) for member in members]
        )

    @classmethod
    def _read_bounded_tar_members(cls, archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
        members: list[tarfile.TarInfo] = []
        expanded_total = 0
        while True:
            member = archive.next()
            if member is None:
                break
            members.append(member)
            if len(members) > MAX_ARCHIVE_MEMBER_COUNT:
                raise ValueError("Backup bundle archive contains too many members.")
            if not member.isfile():
                raise ValueError("Backup bundle TAR archive contains an unsupported member type.")
            cls._normalize_archive_member_path(member.name)
            if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Backup bundle archive member exceeds its expanded byte limit.")
            expanded_total += member.size
            if expanded_total > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ValueError("Backup bundle archive expanded data exceeds its byte limit.")
        return members

    @classmethod
    def _validate_supported_physical_archive_paths(
        cls,
        physical_paths: list[str],
        manifest: dict[str, Any],
    ) -> None:
        expected_paths = {"manifest.json"}
        expected_paths.update(entry["archive_path"] for entry in cls._manifest_file_entries(manifest))
        normalized_physical = {cls._normalize_archive_member_path(path) for path in physical_paths}
        if normalized_physical - expected_paths:
            raise ValueError("Backup bundle archive contains a member not declared in its manifest.")

    @classmethod
    def _validate_7z_listed_entries(
        cls,
        entries: list[dict[str, str]],
        *,
        archive_size: int,
        member_limit: int = MAX_ARCHIVE_MEMBER_BYTES,
        expanded_limit: int = MAX_ARCHIVE_EXPANDED_BYTES,
    ) -> None:
        declared: list[tuple[int, int]] = []
        materialized_paths: set[str] = set()
        for entry in entries:
            normalized_path = cls._normalize_archive_member_path(entry.get("Path", ""))
            path_parts = normalized_path.split("/")
            materialized_paths.add(normalized_path)
            materialized_paths.update(
                "/".join(path_parts[:index])
                for index in range(1, len(path_parts))
            )
            if len(materialized_paths) > MAX_ARCHIVE_MEMBER_COUNT:
                raise ValueError("Backup bundle archive contains too many members.")
            is_directory = cls._is_7z_directory_entry(entry)
            attributes = entry.get("Attributes", "")
            attribute_tokens = attributes.split()
            if (
                entry.get("Symbolic Link")
                or entry.get("Hard Link")
                or any(len(token) >= 10 and token.startswith("l") for token in attribute_tokens)
            ):
                raise ValueError("Backup bundle 7z archive contains an unsupported link.")
            try:
                expanded_size = int(entry.get("Size", "0" if is_directory else ""))
            except ValueError as exc:
                raise ValueError("Backup bundle 7z member size metadata is invalid.") from exc
            raw_packed_size = entry.get("Packed Size", "").strip()
            packed_size = 0
            if raw_packed_size:
                try:
                    packed_size = int(raw_packed_size)
                except ValueError as exc:
                    raise ValueError("Backup bundle 7z packed-size metadata is invalid.") from exc
                if expanded_size > MAX_ARCHIVE_COMPRESSION_RATIO * max(packed_size, 1):
                    raise ValueError(
                        "Backup bundle archive member compression ratio exceeds its limit."
                    )
            declared.append((packed_size, expanded_size))
        cls._validate_declared_archive_limits(
            declared,
            compressed_total=archive_size,
            member_limit=member_limit,
            expanded_limit=expanded_limit,
        )

    @classmethod
    def _validate_7z_listing_against_manifest(
        cls,
        entries: list[dict[str, str]],
        manifest: dict[str, Any],
    ) -> None:
        manifest_entries = {
            cls._normalize_archive_member_path(str(entry["archive_path"])): entry
            for entry in manifest.get("files", [])
            if isinstance(entry, dict) and entry.get("archive_path")
        }
        listed_payload_paths: set[str] = set()
        non_history_total = 0
        for listed_entry in entries:
            if cls._is_7z_directory_entry(listed_entry):
                continue
            archive_path = cls._normalize_archive_member_path(listed_entry["Path"])
            expanded_size = int(listed_entry["Size"])
            if archive_path == "manifest.json":
                non_history_total += expanded_size
                continue

            manifest_entry = manifest_entries.get(archive_path)
            if manifest_entry is None:
                raise ValueError(
                    "Backup bundle archive contains a member not declared in its manifest."
                )
            listed_payload_paths.add(archive_path)
            is_history_member = cls._is_declared_history_manifest_member(
                manifest,
                key=str(manifest_entry.get("key") or "").strip(),
                group_key=str(manifest_entry.get("group_key") or "").strip(),
                archive_path=archive_path,
            )
            if manifest_entry.get("group_key") == HISTORY_DB_KEY and not is_history_member:
                raise ValueError(
                    "Backup bundle history member does not match its declared backup group."
                )
            member_limit = (
                MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES
                if is_history_member
                else MAX_ARCHIVE_MEMBER_BYTES
            )
            if expanded_size > member_limit:
                raise ValueError(
                    "Backup bundle archive member exceeds its expanded byte limit."
                )

            expected_size = manifest_entry.get("size_bytes")
            if expected_size is not None and expanded_size != expected_size:
                raise ValueError(
                    "Backup bundle archive member size does not match its manifest."
                )
            if not is_history_member:
                non_history_total += expanded_size

        if listed_payload_paths != set(manifest_entries):
            raise ValueError("Backup bundle archive members do not match its manifest.")
        if non_history_total > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError(
                "Backup bundle non-history members exceed the expanded byte limit."
            )

    @staticmethod
    def _is_7z_directory_entry(entry: dict[str, str]) -> bool:
        return entry.get("Folder") == "+" or entry.get("Attributes", "").startswith("D")

    @classmethod
    def _validate_extracted_7z_tree(
        cls,
        extract_dir: Path,
        listed_entries: list[dict[str, str]],
    ) -> None:
        expected_sizes = {
            cls._normalize_archive_member_path(entry["Path"]): int(entry["Size"])
            for entry in listed_entries
            if not cls._is_7z_directory_entry(entry)
        }
        expected_directories = {
            cls._normalize_archive_member_path(entry["Path"])
            for entry in listed_entries
            if cls._is_7z_directory_entry(entry)
        }
        actual_sizes: dict[str, int] = {}
        actual_directories: set[str] = set()
        for root, directories, filenames in os.walk(extract_dir, followlinks=False):
            root_path = Path(root)
            for directory in directories:
                directory_path = root_path / directory
                if directory_path.is_symlink():
                    raise ValueError("Backup bundle 7z archive extracted an unsupported link.")
                actual_directories.add(
                    cls._normalize_archive_member_path(
                        directory_path.relative_to(extract_dir).as_posix()
                    )
                )
            for filename in filenames:
                file_path = root_path / filename
                if file_path.is_symlink() or not file_path.is_file():
                    raise ValueError("Backup bundle 7z archive extracted an unsupported file type.")
                relative = cls._normalize_archive_member_path(
                    file_path.relative_to(extract_dir).as_posix()
                )
                actual_sizes[relative] = file_path.stat().st_size
        if actual_sizes != expected_sizes:
            raise ValueError("Backup bundle 7z extracted members do not match its listing.")
        if not expected_directories.issubset(actual_directories):
            raise ValueError("Backup bundle 7z extracted directories do not match its listing.")

    def _extract_manifest_zip_members(
        self,
        archive: zipfile.ZipFile,
        manifest: dict[str, Any],
    ) -> dict[str, ExtractedMember]:
        extracted: dict[str, ExtractedMember] = {}
        for entry in self._manifest_file_entries(manifest):
            try:
                extracted[entry["key"]] = archive.read(entry["archive_path"])
            except KeyError as exc:
                raise ValueError(f"Backup bundle is missing {entry['archive_path']}.") from exc
        return extracted

    def _extract_manifest_tar_members(
        self,
        archive: tarfile.TarFile,
        manifest: dict[str, Any],
    ) -> dict[str, ExtractedMember]:
        extracted: dict[str, ExtractedMember] = {}
        for entry in self._manifest_file_entries(manifest):
            extracted[entry["key"]] = self._read_tar_member(archive, entry["archive_path"])
        return extracted

    def _extract_manifest_directory_members(
        self,
        extract_dir: Path,
        manifest: dict[str, Any],
    ) -> dict[str, ExtractedMember]:
        extracted: dict[str, ExtractedMember] = {}
        for entry in self._manifest_file_entries(manifest):
            member_path = self._safe_child_path(
                extract_dir,
                Path(entry["archive_path"]),
                entry["archive_path"],
            )
            if not member_path.exists():
                raise ValueError(f"Backup bundle is missing {entry['archive_path']}.")
            extracted[entry["key"]] = member_path
        return extracted

    @staticmethod
    def _normalize_archive_member_path(archive_path: str) -> str:
        raw_path = str(archive_path or "").strip()
        normalized_path = raw_path.replace("\\", "/")
        path_parts = normalized_path.split("/")
        posix_path = PurePosixPath(normalized_path)
        windows_path = PureWindowsPath(raw_path)
        if (
            not normalized_path
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
        ):
            raise ValueError(f"Backup bundle archive member path is invalid: {archive_path}")
        if any(part in {"", ".", ".."} or ":" in part for part in path_parts):
            raise ValueError(f"Backup bundle archive member path is invalid: {archive_path}")
        return normalized_path

    @classmethod
    def _validate_unique_physical_archive_paths(cls, archive_paths: list[str]) -> None:
        seen: set[str] = set()
        for archive_path in archive_paths:
            normalized_path = cls._normalize_archive_member_path(archive_path)
            if normalized_path in seen:
                raise ValueError(
                    f"Backup bundle contains duplicate physical archive member {normalized_path!r}."
                )
            seen.add(normalized_path)

    @staticmethod
    def _seven_zip_listed_entries(output: str, archive_path: Path) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                if current:
                    entries.append(current)
                    current = {}
                continue
            if " = " not in line:
                continue
            key, value = line.split(" = ", 1)
            current[key] = value
        if current:
            entries.append(current)

        archive_names = {str(archive_path), archive_path.name}
        return [
            entry
            for entry in entries
            if entry.get("Path")
            and not (
                entry["Path"] in archive_names
                and entry.get("Type") == "7z"
            )
        ]

    @staticmethod
    def _safe_child_path(root_dir: Path, relative_path: Path, archive_path: str) -> Path:
        target_path = root_dir / relative_path
        try:
            target_path.resolve(strict=False).relative_to(root_dir.resolve(strict=False))
        except ValueError as exc:
            raise ValueError(f"Backup bundle archive member path is invalid: {archive_path}") from exc
        return target_path

    @classmethod
    def _manifest_file_entries(cls, manifest: dict[str, Any]) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for index, raw_entry in enumerate(manifest.get("files", [])):
            if not isinstance(raw_entry, dict):
                continue
            raw_archive_path = str(raw_entry.get("archive_path") or "").strip()
            if not raw_archive_path:
                continue
            archive_path = cls._normalize_archive_member_path(raw_archive_path)
            key = str(raw_entry.get("key") or archive_path or f"member-{index}").strip()
            group_key = str(raw_entry.get("group_key") or raw_entry.get("key") or key).strip()
            entries.append(
                {
                    "key": key,
                    "group_key": group_key,
                    "archive_path": archive_path,
                }
            )
        return entries

    def _manifest_group_entries(self, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw_groups = manifest.get("groups")
        if isinstance(raw_groups, list):
            groups: dict[str, dict[str, Any]] = {}
            for raw_entry in raw_groups:
                if not isinstance(raw_entry, dict):
                    continue
                key = str(raw_entry.get("key") or "").strip()
                if not key:
                    continue
                groups[key] = dict(raw_entry)
            if groups:
                return groups

        legacy_groups: dict[str, dict[str, Any]] = {}
        for raw_entry in manifest.get("files", []):
            if not isinstance(raw_entry, dict):
                continue
            key = str(raw_entry.get("key") or "").strip()
            if not key:
                continue
            metadata = BACKUP_GROUP_METADATA.get(key)
            legacy_groups[key] = {
                "key": key,
                "label": metadata["label"] if metadata else key,
                "archive_root": raw_entry.get("archive_path"),
                "source_path": raw_entry.get("source_path"),
                "selected": True,
                "present": bool(raw_entry.get("present", True)),
                "sensitive": bool(metadata["sensitive"]) if metadata else False,
                "restore_mode": metadata["restore_mode"] if metadata else "file",
            }
        return legacy_groups

    @staticmethod
    def _manifest_group_selected(group_entry: dict[str, Any] | None) -> bool:
        if not group_entry:
            return False
        return bool(group_entry.get("selected", True))

    @staticmethod
    def _manifest_group_present(group_entry: dict[str, Any] | None) -> bool:
        if not group_entry:
            return False
        return bool(group_entry.get("present", False))

    def _group_members(self, manifest: dict[str, Any], group_key: str) -> list[dict[str, str]]:
        return [
            entry
            for entry in self._manifest_file_entries(manifest)
            if entry["group_key"] == group_key
        ]

    def _first_group_member(self, manifest: dict[str, Any], group_key: str) -> dict[str, str] | None:
        members = self._group_members(manifest, group_key)
        return members[0] if members else None

    def _restore_file_group(
        self,
        group_key: str,
        manifest: dict[str, Any],
        group_entries: dict[str, dict[str, Any]],
        extracted_members: dict[str, ExtractedMember],
        target_path: Path,
        restored_paths: list[str],
        transaction: _ImportActivationTransaction | None = None,
    ) -> None:
        group_entry = group_entries.get(group_key)
        if not self._manifest_group_selected(group_entry):
            return
        member_entries = self._group_members(manifest, group_key)
        if member_entries:
            member_key = member_entries[0]["key"]
            if member_key not in extracted_members:
                raise ValueError(f"Backup bundle is missing the selected {group_key} member.")
            if transaction is None:
                self._write_bytes_atomic(
                    target_path,
                    self._extracted_member_bytes(extracted_members[member_key]),
                )
            else:
                transaction.activate_file(target_path, member_key)
            restored_paths.append(str(target_path))
            return
        if self._manifest_group_present(group_entry):
            raise ValueError(f"Backup bundle is missing the selected {group_key} member.")
        # `present=false` describes the source backup. It is not permission to
        # delete data that may have been created after that backup was taken.

    def _restore_directory_group(
        self,
        group_key: str,
        manifest: dict[str, Any],
        group_entries: dict[str, dict[str, Any]],
        extracted_members: dict[str, ExtractedMember],
        target_dir: Path,
        restored_paths: list[str],
        transaction: _ImportActivationTransaction | None = None,
    ) -> None:
        group_entry = group_entries.get(group_key)
        if not self._manifest_group_selected(group_entry):
            return
        member_entries = self._group_members(manifest, group_key)
        if not member_entries:
            if self._manifest_group_present(group_entry):
                raise ValueError(f"Backup bundle is missing the selected {group_key} directory members.")
            # Preserve newer live keys or trust material when an older backup
            # selected this group but had no directory to export.
            return

        restore_targets: list[tuple[str, Path]] = []
        transaction_members: list[tuple[str, Path]] = []
        for entry in member_entries:
            relative_path = self._directory_member_relative_path(group_key, entry["archive_path"])
            target_path = self._safe_child_path(target_dir, relative_path, entry["archive_path"])
            member_key = entry["key"]
            if member_key not in extracted_members:
                raise ValueError(f"Backup bundle is missing the selected {group_key} member {entry['archive_path']}.")
            restore_targets.append((member_key, target_path))
            transaction_members.append((member_key, relative_path))

        if transaction is None:
            self._remove_tree_if_exists(target_dir)
            for member_key, target_path in restore_targets:
                self._write_bytes_atomic(
                    target_path,
                    self._extracted_member_bytes(extracted_members[member_key]),
                )
        else:
            transaction.activate_directory(target_dir, transaction_members)
        restored_paths.extend(str(target_path) for _member_key, target_path in restore_targets)

    @staticmethod
    def _directory_member_relative_path(group_key: str, archive_path: str) -> Path:
        archive_root = str(BACKUP_GROUP_METADATA[group_key]["archive_root"]).strip("/")
        try:
            normalized_archive_path = SystemBackupService._normalize_archive_member_path(archive_path)
            normalized_archive_root = (
                SystemBackupService._normalize_archive_member_path(archive_root)
                if archive_root
                else ""
            )
        except ValueError as exc:
            raise ValueError(f"Backup bundle directory member path is invalid: {archive_path}") from exc
        if normalized_archive_root and normalized_archive_path.startswith(f"{normalized_archive_root}/"):
            relative_text = normalized_archive_path[len(normalized_archive_root) + 1 :]
        else:
            relative_text = normalized_archive_path
        try:
            relative_text = SystemBackupService._normalize_archive_member_path(relative_text)
        except ValueError as exc:
            raise ValueError(f"Backup bundle directory member path is invalid: {archive_path}") from exc
        relative_path = Path(relative_text)
        if not relative_text or any(part in {"..", "", "."} for part in relative_path.parts):
            raise ValueError(f"Backup bundle directory member path is invalid: {archive_path}")
        return relative_path

    @staticmethod
    def _add_tar_member(archive: tarfile.TarFile, archive_path: str, content: bytes) -> None:
        info = tarfile.TarInfo(name=archive_path)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

    @staticmethod
    def _read_tar_member(archive: tarfile.TarFile, archive_path: str) -> bytes:
        try:
            member = archive.getmember(archive_path)
        except KeyError as exc:
            raise ValueError(f"Backup bundle is missing {archive_path}.") from exc
        extracted = archive.extractfile(member)
        if extracted is None:
            raise ValueError(f"Backup bundle member {archive_path} could not be read.")
        return extracted.read()

    @staticmethod
    def _normalize_packaging(packaging: str) -> ArchivePackaging:
        normalized = str(packaging or "").strip().lower()
        if normalized not in SUPPORTED_ARCHIVE_PACKAGING:
            raise ValueError(f"Unsupported backup packaging '{packaging}'.")
        return normalized  # type: ignore[return-value]

    @classmethod
    def _detect_archive_packaging(
        cls,
        archive_bytes: bytes,
    ) -> ArchivePackaging | None:
        if archive_bytes.startswith(b"PK"):
            return "zip"
        if archive_bytes.startswith(b"\x1f\x8b"):
            return "tar.gz"
        if archive_bytes.startswith(b"\x28\xb5\x2f\xfd"):
            return "tar.zst"
        if archive_bytes.startswith(SEVEN_ZIP_SIGNATURE):
            return "7z"
        return None

    @classmethod
    def _decompress_single_gzip_member(cls, archive_bytes: bytes) -> bytes:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        content = bytearray()
        ratio_limit = MAX_ARCHIVE_COMPRESSION_RATIO * max(len(archive_bytes), 1)
        output_limit = min(MAX_ARCHIVE_EXPANDED_BYTES, ratio_limit)

        def reject_output_limit() -> None:
            if ratio_limit <= MAX_ARCHIVE_EXPANDED_BYTES:
                raise ValueError("Backup bundle archive compression ratio exceeds its limit.")
            raise ValueError("Backup bundle archive expanded data exceeds its byte limit.")

        for offset in range(0, len(archive_bytes), ARCHIVE_READ_CHUNK_BYTES):
            chunk = archive_bytes[offset : offset + ARCHIVE_READ_CHUNK_BYTES]
            content.extend(
                decompressor.decompress(
                    chunk,
                    output_limit - len(content) + 1,
                )
            )
            if len(content) > output_limit:
                reject_output_limit()
            if decompressor.eof:
                trailing = decompressor.unused_data + archive_bytes[offset + len(chunk) :]
                if trailing:
                    raise ValueError("Backup bundle tar.gz archive contains concatenated gzip data.")
                break
        if not decompressor.eof:
            raise ValueError("Backup bundle tar.gz archive is corrupted.")
        content.extend(decompressor.flush(output_limit - len(content) + 1))
        if len(content) > output_limit:
            reject_output_limit()
        return bytes(content)

    @classmethod
    def _decompress_single_zstd_frame(cls, archive_bytes: bytes) -> bytes:
        if zstd is None:
            raise ValueError("tar.zst import requires the optional 'zstandard' dependency.")
        declared_size = zstd.frame_content_size(archive_bytes)
        if declared_size in {zstd.CONTENTSIZE_ERROR, zstd.CONTENTSIZE_UNKNOWN}:
            raise ValueError("Backup bundle tar.zst archive has unsupported frame metadata.")
        if declared_size > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError("Backup bundle archive expanded data exceeds its byte limit.")
        if declared_size > MAX_ARCHIVE_COMPRESSION_RATIO * max(len(archive_bytes), 1):
            raise ValueError("Backup bundle archive compression ratio exceeds its limit.")
        decompressor = zstd.ZstdDecompressor().decompressobj()
        content = decompressor.decompress(archive_bytes)
        if not decompressor.eof:
            raise ValueError("Backup bundle tar.zst archive is corrupted.")
        if decompressor.unused_data:
            raise ValueError("Backup bundle tar.zst archive contains concatenated zstd data.")
        content += decompressor.flush()
        if len(content) != declared_size:
            raise ValueError("Backup bundle tar.zst archive size does not match its frame metadata.")
        return content

    @classmethod
    def _decompress_tar_archive(
        cls,
        archive_bytes: bytes,
        packaging: ArchivePackaging,
    ) -> bytes:
        if packaging == "tar.gz":
            try:
                tar_bytes = cls._decompress_single_gzip_member(archive_bytes)
            except zlib.error as exc:
                raise ValueError("Backup bundle tar.gz archive is corrupted.") from exc
        elif packaging == "tar.zst":
            if zstd is None:
                raise ValueError("tar.zst import requires the optional 'zstandard' dependency.")
            try:
                tar_bytes = cls._decompress_single_zstd_frame(archive_bytes)
            except zstd.ZstdError as exc:
                raise ValueError("Backup bundle tar.zst archive is corrupted.") from exc
        else:
            raise ValueError(f"Unsupported tar archive packaging '{packaging}'.")
        if len(tar_bytes) > MAX_ARCHIVE_COMPRESSION_RATIO * max(len(archive_bytes), 1):
            raise ValueError("Backup bundle archive compression ratio exceeds its limit.")
        return tar_bytes

    def _run_7z_command(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        passphrase: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            command = [SEVEN_ZIP_BINARY, *args]
            if passphrase is not None:
                return self._run_7z_prompt_command(
                    command,
                    cwd=cwd,
                    passphrase=passphrase,
                )
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None or process.stderr is None:
                process.kill()
                raise ValueError("Portable 7z backup command output could not be captured.")

            overflow = threading.Event()
            read_errors: list[Exception] = []
            stdout_content = bytearray()
            stderr_content = bytearray()

            def read_bounded(stream: Any, target: bytearray) -> None:
                try:
                    while True:
                        remaining = MAX_7Z_COMMAND_OUTPUT_BYTES - len(target)
                        chunk = stream.read(min(ARCHIVE_READ_CHUNK_BYTES, remaining + 1))
                        if not chunk:
                            return
                        target.extend(chunk)
                        if len(target) > MAX_7Z_COMMAND_OUTPUT_BYTES:
                            overflow.set()
                            process.kill()
                            return
                except Exception as exc:  # pragma: no cover - OS pipe failures are rare
                    read_errors.append(exc)
                    process.kill()

            stdout_thread = threading.Thread(
                target=read_bounded,
                args=(process.stdout, stdout_content),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=read_bounded,
                args=(process.stderr, stderr_content),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                returncode = process.wait(timeout=SEVEN_ZIP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            finally:
                stdout_thread.join()
                stderr_thread.join()
                process.stdout.close()
                process.stderr.close()

            if overflow.is_set():
                raise ValueError("Portable 7z backup command output exceeded its byte limit.")
            if read_errors:
                raise ValueError("Portable 7z backup command output could not be read.") from read_errors[0]
            return subprocess.CompletedProcess(
                command,
                returncode,
                stdout=stdout_content.decode("utf-8", errors="replace"),
                stderr=stderr_content.decode("utf-8", errors="replace"),
            )
        except FileNotFoundError as exc:
            raise ValueError(
                "Portable 7z backup support requires the '7z' command inside the container image."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Portable 7z backup operation timed out.") from exc

    @staticmethod
    def _run_7z_prompt_command(
        command: list[str],
        *,
        cwd: Path | None,
        passphrase: str,
    ) -> subprocess.CompletedProcess[str]:
        if "\n" in passphrase or "\r" in passphrase:
            raise ValueError("Portable 7z backup passphrases cannot contain line breaks.")
        try:
            import errno
            import pty
        except ImportError as exc:  # pragma: no cover - production images are Linux-based.
            raise ValueError("Portable encrypted 7z backup support requires a POSIX terminal.") from exc

        master_fd, slave_fd = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        output = bytearray()
        prompt_seen = False
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd is not None else None,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            deadline = time.monotonic() + SEVEN_ZIP_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(command, SEVEN_ZIP_TIMEOUT_SECONDS)
                ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.1))
                if ready:
                    try:
                        chunk = os.read(master_fd, ARCHIVE_READ_CHUNK_BYTES)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_7Z_COMMAND_OUTPUT_BYTES:
                        process.kill()
                        process.wait()
                        raise ValueError("Portable 7z backup command output exceeded its byte limit.")
                    if not prompt_seen and b"Enter password" in output:
                        os.write(master_fd, passphrase.encode("utf-8") + b"\n")
                        prompt_seen = True
                if process.poll() is not None and not ready:
                    break
            returncode = process.wait(timeout=max(deadline - time.monotonic(), 0.001))
            return subprocess.CompletedProcess(
                command,
                returncode,
                stdout=output.decode("utf-8", errors="replace"),
                stderr="",
            )
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if slave_fd >= 0:
                os.close(slave_fd)
            os.close(master_fd)

    @staticmethod
    def _raise_for_7z_failure(
        result: subprocess.CompletedProcess[str],
        message: str,
        *,
        passphrase: str | None,
        reading_archive: bool = False,
    ) -> None:
        if result.returncode == 0:
            return
        combined_output = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        if "Wrong password?" in combined_output or "Cannot open encrypted archive" in combined_output:
            if passphrase:
                raise ValueError("Unable to decrypt backup bundle. Check the passphrase and try again.")
            raise ValueError("This backup is encrypted and requires a passphrase.")
        if reading_archive and (
            "Headers Error" in combined_output or "Can't open as archive" in combined_output
        ):
            raise ValueError("Backup bundle 7z archive is corrupted.")
        detail = f"{message} {combined_output}".strip()
        raise ValueError(detail)

    @staticmethod
    def _write_bytes_atomic(target_path: Path, content: bytes) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
        temp_path.write_bytes(content)
        temp_path.replace(target_path)

    @staticmethod
    def _remove_tree_if_exists(target_dir: Path) -> None:
        if not target_dir.exists():
            return
        for file_path in sorted((path for path in target_dir.rglob("*") if path.is_file()), reverse=True):
            file_path.unlink(missing_ok=True)
        for directory in sorted((path for path in target_dir.rglob("*") if path.is_dir()), reverse=True):
            directory.rmdir()
        target_dir.rmdir()

    def _read_scrubbed_yaml_file(self, path: Path, scrubber: DebugScrubber | None) -> bytes:
        if not path.exists():
            return b""
        raw_text = path.read_text(encoding="utf-8")
        if scrubber is None:
            return raw_text.encode("utf-8")
        payload = yaml.safe_load(raw_text) or {}
        scrubbed = scrubber.scrub_payload(payload)
        return yaml.safe_dump(
            scrubbed,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=False,
        ).encode("utf-8")

    def _read_scrubbed_json_file(self, path: Path, scrubber: DebugScrubber | None) -> bytes:
        if not path.exists():
            return b""
        payload = json.loads(path.read_text(encoding="utf-8"))
        if scrubber is not None:
            payload = scrubber.scrub_payload(payload)
        return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")

    def _build_scrubbed_history_snapshot_file(
        self,
        snapshot_path: Path,
        scrubber: DebugScrubber,
        target_path: Path,
    ) -> Path:
        shutil.copyfile(snapshot_path, target_path)
        connection = sqlite3.connect(target_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA secure_delete=ON")
            (
                slot_state_rules,
                slot_event_rules,
                metric_sample_rules,
                metric_rollup_rules,
            ) = self._history_scrubbing_rules(scrubber)
            if slot_state_rules:
                self._scrub_history_table(connection, "slot_state_current", slot_state_rules)
            if slot_event_rules:
                self._scrub_history_table(connection, "slot_events", slot_event_rules)
            if metric_sample_rules:
                self._scrub_history_table(connection, "metric_samples", metric_sample_rules)
            if metric_rollup_rules:
                self._scrub_history_table(connection, "metric_rollups", metric_rollup_rules)
            connection.commit()
            connection.execute("VACUUM")
        finally:
            connection.close()
        return target_path

    @staticmethod
    def _history_scrubbing_rules(
        scrubber: DebugScrubber,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        slot_state_rules: dict[str, Any] = {}
        slot_event_rules: dict[str, Any] = {}
        metric_sample_rules: dict[str, Any] = {}
        metric_rollup_rules: dict[str, Any] = {}
        if scrubber.scrub_disk_identifiers:
            common_rules = {
                "device_name": scrubber.alias_device_name,
                "serial": lambda value: scrubber.alias_identifier("serial", value),
                "gptid": lambda value: scrubber.alias_identifier("gptid", value),
                "persistent_id_label": lambda value: scrubber.alias_identifier("persistent_id", value),
                "disk_identity_key": lambda value: scrubber.alias_identifier("disk_identity_key", value),
                "logical_unit_id": lambda value: scrubber.alias_identifier("logical_unit_id", value),
                "sas_address": lambda value: scrubber.alias_identifier("sas_address", value),
            }
            slot_state_rules.update(
                {
                    **common_rules,
                    "multipath_device": scrubber.alias_device_name,
                    "multipath_lunid": lambda value: scrubber.alias_identifier("multipath_lunid", value),
                }
            )
            slot_event_rules.update(common_rules)
            metric_sample_rules.update(common_rules)
            metric_rollup_rules.update(common_rules)
        if scrubber.scrub_secrets or scrubber.scrub_disk_identifiers:
            slot_event_rules["details_json"] = scrubber.scrub_json_text
        return slot_state_rules, slot_event_rules, metric_sample_rules, metric_rollup_rules

    @staticmethod
    def _scrub_history_table(
        connection: sqlite3.Connection,
        table_name: str,
        scrubbing_rules: dict[str, Any],
    ) -> None:
        cursor = connection.execute(f"SELECT rowid AS _rowid_, * FROM {table_name}")
        while rows := cursor.fetchmany(500):
            for row in rows:
                updates: dict[str, Any] = {}
                for column_name, scrubber in scrubbing_rules.items():
                    if column_name not in row.keys():
                        continue
                    value = row[column_name]
                    if value in {None, ""}:
                        continue
                    scrubbed = scrubber(value)
                    if scrubbed != value:
                        updates[column_name] = scrubbed
                if not updates:
                    continue
                set_clause = ", ".join(f"{column} = ?" for column in updates)
                parameters = [updates[column] for column in updates] + [row["_rowid_"]]
                connection.execute(
                    f"UPDATE {table_name} SET {set_clause} WHERE rowid = ?",
                    parameters,
                )

    def _build_debug_state_bytes(
        self,
        app_settings: Settings,
        *,
        runtime_payload: dict[str, Any] | None,
        maintenance_payload: dict[str, Any] | None,
        selected_groups: list[str],
        scrubber: DebugScrubber | None,
        exported_at: datetime,
    ) -> bytes:
        payload = {
            "exported_at": exported_at.isoformat(),
            "app_version": __version__,
            "selected_groups": list(selected_groups),
            "default_system_id": app_settings.default_system_id,
            "systems": [
                {
                    "id": system.id,
                    "label": system.label,
                    "platform": system.truenas.platform,
                    "default_profile_id": system.default_profile_id,
                    "storage_view_count": len(system.storage_views),
                    "storage_views": [
                        {
                            "id": view.id,
                            "label": view.label,
                            "kind": view.kind,
                            "template_id": view.template_id,
                            "profile_id": view.profile_id,
                            "binding_mode": view.binding.mode,
                        }
                        for view in system.storage_views
                    ],
                    "truenas": {
                        "host": system.truenas.host,
                        "verify_ssl": system.truenas.verify_ssl,
                        "tls_ca_bundle_path": system.truenas.tls_ca_bundle_path,
                        "tls_server_name": system.truenas.tls_server_name,
                    },
                    "ssh": {
                        "enabled": system.ssh.enabled,
                        "host": system.ssh.host,
                        "extra_hosts": list(system.ssh.extra_hosts),
                        "user": system.ssh.user,
                        "key_path": system.ssh.key_path,
                        "known_hosts_path": system.ssh.known_hosts_path,
                        "commands": list(system.ssh.commands),
                    },
                }
                for system in app_settings.systems
            ],
            "paths": describe_bundle_groups(app_settings, self.history_settings),
            "history_counts": self.store.counts(),
            "runtime": runtime_payload,
            "maintenance": maintenance_payload,
        }
        if scrubber is not None:
            payload = scrubber.scrub_payload(payload)
        return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")

    @staticmethod
    def _build_debug_readme_bytes(
        *,
        scrub_secrets: bool,
        scrub_disk_identifiers: bool,
    ) -> bytes:
        lines = [
            "truenas-jbod-ui debug bundle",
            "",
            "This archive is a support/debug snapshot, not a restore bundle.",
            "Use the full backup export if you need a portable restore path.",
            "Open it with normal archive tools for offline inspection.",
            "There is no debug-bundle import or replay flow today.",
            "",
            f"Secrets scrub: {'enabled' if scrub_secrets else 'disabled'}",
            f"Disk identifier scrub: {'enabled' if scrub_disk_identifiers else 'disabled'}",
            "Scrubbing is best-effort and focuses on obvious secrets, connection details, and disk identity fields.",
        ]
        return "\n".join(lines).encode("utf-8")
