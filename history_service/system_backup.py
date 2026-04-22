from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - exercised in runtime validation instead.
    zstd = None

from app import __version__
from app.config import Settings, _derive_runtime_layout_paths, get_settings
from history_service.config import HistorySettings
from history_service.store import HistoryStore


BUNDLE_SCHEMA_VERSION = 1
BUNDLE_FORMAT = "truenas-jbod-ui-backup"
DEBUG_BUNDLE_FORMAT = "truenas-jbod-ui-debug-bundle"

CONFIG_FILE_KEY = "config_file"
PROFILE_FILE_KEY = "profile_file"
MAPPING_FILE_KEY = "mapping_file"
SLOT_DETAIL_FILE_KEY = "slot_detail_file"
HISTORY_DB_KEY = "history_db"
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
SEVEN_ZIP_TIMEOUT_SECONDS = 120
SEVEN_ZIP_BINARY = "7z"


@dataclass(slots=True)
class BundleMember:
    key: str
    group_key: str
    archive_path: str
    source_path: str | None
    present: bool
    content: bytes | None


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
        PROFILE_FILE_KEY: app_settings.paths.profile_file,
        MAPPING_FILE_KEY: app_settings.paths.mapping_file,
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
        "profile_file",
        "mapping_file",
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

    def export_bundle(
        self,
        *,
        encrypt: bool = False,
        passphrase: str | None = None,
        packaging: ArchivePackaging = "tar.zst",
        included_paths: list[str] | None = None,
    ) -> BackupArtifact:
        app_settings = self._load_app_settings()
        exported_at = datetime.now(timezone.utc)
        history_snapshot_bytes = self._build_history_snapshot()
        selected_groups = self._resolve_selected_groups(included_paths, bundle_type="backup")
        self._validate_encrypted_scope(selected_groups, encrypt=encrypt)
        requested_packaging = self._normalize_packaging(packaging)
        if encrypt and not passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        normalized_packaging: ArchivePackaging = "7z" if encrypt else requested_packaging
        bundle_groups, bundle_members = self._collect_backup_bundle(
            app_settings,
            history_snapshot_bytes,
            selected_groups=selected_groups,
        )
        manifest = self._build_manifest(
            format_name=BUNDLE_FORMAT,
            app_settings=app_settings,
            exported_at=exported_at,
            packaging=normalized_packaging,
            bundle_groups=bundle_groups,
            bundle_members=bundle_members,
        )
        archive_bytes = self._build_archive(
            bundle_members,
            manifest,
            normalized_packaging,
            passphrase=passphrase if encrypt else None,
        )
        stem = f"jbod-system-backup-{exported_at.strftime('%Y%m%dT%H%M%SZ')}"
        return BackupArtifact(
            filename=f"{stem}{ARCHIVE_FILE_SUFFIXES[normalized_packaging]}",
            content=archive_bytes,
            media_type=ARCHIVE_MEDIA_TYPES[normalized_packaging],
            manifest=manifest,
        )

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
        app_settings = self._load_app_settings()
        exported_at = datetime.now(timezone.utc)
        history_snapshot_bytes = self._build_history_snapshot()
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
        bundle_groups, bundle_members = self._collect_debug_bundle(
            app_settings,
            history_snapshot_bytes,
            selected_groups=selected_groups,
            scrubber=scrubber,
            runtime_payload=runtime_payload,
            maintenance_payload=maintenance_payload,
            exported_at=exported_at,
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
        archive_bytes = self._build_archive(
            bundle_members,
            manifest,
            normalized_packaging,
            passphrase=passphrase if encrypt else None,
        )
        stem = f"jbod-debug-bundle-{exported_at.strftime('%Y%m%dT%H%M%SZ')}"
        return BackupArtifact(
            filename=f"{stem}{ARCHIVE_FILE_SUFFIXES[normalized_packaging]}",
            content=archive_bytes,
            media_type=ARCHIVE_MEDIA_TYPES[normalized_packaging],
            manifest=manifest,
        )

    def import_bundle(self, content: bytes, *, passphrase: str | None = None) -> dict[str, Any]:
        manifest, extracted, detected_packaging, archive_meta = self._read_archive(
            content,
            passphrase=passphrase,
        )
        if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported backup schema version {manifest.get('schema_version')!r}."
            )
        if manifest.get("format") != BUNDLE_FORMAT:
            raise ValueError("Backup bundle format is not recognized.")

        group_entries = self._manifest_group_entries(manifest)
        restored_paths: list[str] = []
        app_settings = self._load_app_settings()

        self._restore_file_group(
            CONFIG_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(app_settings.config_file),
            restored_paths,
        )

        imported_settings = self._load_app_settings()
        self._restore_file_group(
            PROFILE_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.profile_file),
            restored_paths,
        )
        self._restore_file_group(
            MAPPING_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.mapping_file),
            restored_paths,
        )
        self._restore_file_group(
            SLOT_DETAIL_FILE_KEY,
            manifest,
            group_entries,
            extracted,
            Path(imported_settings.paths.slot_detail_cache_file),
            restored_paths,
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
        )
        self._restore_directory_group(
            TLS_TRUST_KEY,
            manifest,
            group_entries,
            extracted,
            config_root / "tls",
            restored_paths,
        )
        known_hosts_target = Path(_derive_runtime_layout_paths(imported_settings.config_file)["known_hosts_path"])
        self._restore_file_group(
            KNOWN_HOSTS_KEY,
            manifest,
            group_entries,
            extracted,
            known_hosts_target,
            restored_paths,
        )

        history_restored = False
        history_group = group_entries.get(HISTORY_DB_KEY)
        if self._manifest_group_selected(history_group):
            history_member = self._first_group_member(manifest, HISTORY_DB_KEY)
            if history_member and history_member["key"] in extracted:
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_history_path = Path(temp_dir) / "history-import.sqlite3"
                    temp_history_path.write_bytes(extracted[history_member["key"]])
                    self.store.restore_backup(temp_history_path)
                restored_paths.append(str(self.store.file_path))
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
        }

    def _load_app_settings(self) -> Settings:
        get_settings.cache_clear()
        return get_settings()

    def _build_history_snapshot(self) -> bytes:
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = self.store.create_backup(
                temp_dir,
                retention_count=1,
                long_term_backup_dir=None,
                weekly_retention_count=0,
                monthly_retention_count=0,
            )
            if backup_path is None:
                return b""
            return Path(backup_path).read_bytes()

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
        history_snapshot_bytes: bytes,
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
            elif group_key == SLOT_DETAIL_FILE_KEY:
                group, members = self._collect_file_group(
                    group_key,
                    Path(app_settings.paths.slot_detail_cache_file),
                    selected=selected,
                )
            elif group_key == HISTORY_DB_KEY:
                group, members = self._collect_generated_file_group(
                    group_key,
                    history_snapshot_bytes,
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
        history_snapshot_bytes: bytes,
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
            elif group_key == SLOT_DETAIL_FILE_KEY:
                content_bytes = self._read_scrubbed_json_file(Path(app_settings.paths.slot_detail_cache_file), scrubber)
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
                    selected=selected,
                    source_path=app_settings.paths.slot_detail_cache_file,
                )
            elif group_key == HISTORY_DB_KEY:
                content_bytes = (
                    self._build_scrubbed_history_snapshot(history_snapshot_bytes, scrubber)
                    if scrubber is not None
                    else history_snapshot_bytes
                )
                group, members = self._collect_generated_file_group(
                    group_key,
                    content_bytes,
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
            "files": self._collect_file_specs(bundle_members),
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

    @staticmethod
    def _collect_file_specs(bundle_members: list[BundleMember]) -> list[dict[str, Any]]:
        manifest_files: list[dict[str, Any]] = []
        for member in bundle_members:
            content_bytes = member.content or b""
            manifest_files.append(
                {
                    "key": member.key,
                    "group_key": member.group_key,
                    "archive_path": member.archive_path,
                    "source_path": member.source_path,
                    "size_bytes": len(content_bytes),
                    "sha256": hashlib.sha256(content_bytes).hexdigest() if member.present else None,
                }
            )
        return manifest_files

    def _build_archive(
        self,
        bundle_members: list[BundleMember],
        manifest: dict[str, Any],
        packaging: ArchivePackaging,
        *,
        passphrase: str | None = None,
    ) -> bytes:
        if passphrase is not None and not passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        if packaging == "zip":
            buffer = io.BytesIO()
            with zipfile.ZipFile(
                buffer,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                archive.writestr("manifest.json", manifest_bytes)
                for member in bundle_members:
                    if member.present and member.content is not None:
                        archive.writestr(member.archive_path, member.content)
            return buffer.getvalue()
        if packaging == "7z":
            return self._build_7z_archive(bundle_members, manifest_bytes, passphrase=passphrase)

        tar_bytes = self._build_tar_archive(bundle_members, manifest_bytes)
        if packaging == "tar.gz":
            return gzip.compress(tar_bytes, compresslevel=9)
        if packaging == "tar.zst":
            if zstd is None:
                raise ValueError("tar.zst export requires the optional 'zstandard' dependency.")
            return zstd.ZstdCompressor(level=9).compress(tar_bytes)
        raise ValueError(f"Unsupported backup packaging '{packaging}'.")

    def _build_tar_archive(self, bundle_members: list[BundleMember], manifest_bytes: bytes) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            self._add_tar_member(archive, "manifest.json", manifest_bytes)
            for member in bundle_members:
                if member.present and member.content is not None:
                    self._add_tar_member(archive, member.archive_path, member.content)
        return buffer.getvalue()

    def _build_7z_archive(
        self,
        bundle_members: list[BundleMember],
        manifest_bytes: bytes,
        *,
        passphrase: str | None = None,
    ) -> bytes:
        with tempfile.TemporaryDirectory() as temp_dir:
            staging_dir = Path(temp_dir) / "bundle"
            staging_dir.mkdir(parents=True, exist_ok=True)
            (staging_dir / "manifest.json").write_bytes(manifest_bytes)
            top_level_entries = {"manifest.json"}

            for member in bundle_members:
                if not member.present or member.content is None:
                    continue
                target_path = staging_dir / member.archive_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(member.content)
                top_level_entries.add(member.archive_path.split("/", 1)[0])

            archive_path = Path(temp_dir) / "bundle.7z"
            command = [
                "a",
                "-t7z",
                "-y",
                "-bd",
                "-mx=9",
                "-m0=lzma2",
                str(archive_path),
                *sorted(top_level_entries),
            ]
            if passphrase is not None:
                command.insert(4, "-mhe=on")
                command.insert(4, f"-p{passphrase}")
            result = self._run_7z_command(command, cwd=staging_dir)
            self._raise_for_7z_failure(
                result,
                "Portable 7z backup export failed.",
                passphrase=passphrase,
            )
            return archive_path.read_bytes()

    def _read_archive(
        self,
        archive_bytes: bytes,
        *,
        passphrase: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, bytes], ArchivePackaging, dict[str, Any]]:
        packaging = self._detect_archive_packaging(archive_bytes)
        if not packaging:
            raise ValueError("Backup bundle archive format is not supported.")

        if packaging == "7z":
            return self._read_7z_archive(archive_bytes, passphrase=passphrase)

        if packaging == "zip":
            try:
                with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
                    try:
                        manifest_bytes = archive.read("manifest.json")
                    except KeyError as exc:
                        raise ValueError("Backup bundle is missing manifest.json.") from exc
                    manifest = self._load_manifest(manifest_bytes)
                    extracted = self._extract_manifest_zip_members(archive, manifest)
            except zipfile.BadZipFile as exc:
                raise ValueError("Backup bundle ZIP archive is corrupted.") from exc
            return manifest, extracted, packaging, {"encrypted": False}

        tar_bytes = self._decompress_tar_archive(archive_bytes, packaging)
        try:
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
                manifest = self._load_manifest(self._read_tar_member(archive, "manifest.json"))
                extracted = self._extract_manifest_tar_members(archive, manifest)
        except tarfile.TarError as exc:
            raise ValueError("Backup bundle TAR archive is corrupted.") from exc
        return manifest, extracted, packaging, {"encrypted": False}

    def _read_7z_archive(
        self,
        archive_bytes: bytes,
        *,
        passphrase: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, bytes], ArchivePackaging, dict[str, Any]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            archive_path = temp_root / "bundle.7z"
            extract_dir = temp_root / "extract"
            archive_path.write_bytes(archive_bytes)
            extract_dir.mkdir(parents=True, exist_ok=True)

            list_result = self._run_7z_command(
                [
                    "l",
                    "-slt",
                    str(archive_path),
                    f"-p{passphrase or ''}",
                ]
            )
            self._raise_for_7z_failure(
                list_result,
                "Backup bundle 7z archive could not be listed.",
                passphrase=passphrase,
                reading_archive=True,
            )
            encrypted = "Encrypted = +" in list_result.stdout or "7zAES" in list_result.stdout

            extract_result = self._run_7z_command(
                [
                    "x",
                    str(archive_path),
                    f"-o{extract_dir}",
                    "-y",
                    "-bd",
                    f"-p{passphrase or ''}",
                ]
            )
            self._raise_for_7z_failure(
                extract_result,
                "Backup bundle 7z archive could not be extracted.",
                passphrase=passphrase,
                reading_archive=True,
            )

            manifest_path = extract_dir / "manifest.json"
            if not manifest_path.exists():
                raise ValueError("Backup bundle is missing manifest.json.")
            manifest = self._load_manifest(manifest_path.read_bytes())
            extracted = self._extract_manifest_directory_members(extract_dir, manifest)
            return manifest, extracted, "7z", {"encrypted": encrypted}

    @staticmethod
    def _load_manifest(manifest_bytes: bytes) -> dict[str, Any]:
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Backup bundle manifest is not valid JSON.") from exc
        if not isinstance(manifest, dict):
            raise ValueError("Backup bundle manifest is not valid JSON.")
        return manifest

    def _extract_manifest_zip_members(
        self,
        archive: zipfile.ZipFile,
        manifest: dict[str, Any],
    ) -> dict[str, bytes]:
        extracted: dict[str, bytes] = {}
        for entry in self._manifest_file_entries(manifest):
            try:
                extracted[entry["key"]] = archive.read(entry["archive_path"])
            except KeyError:
                extracted[entry["key"]] = b""
        return extracted

    def _extract_manifest_tar_members(
        self,
        archive: tarfile.TarFile,
        manifest: dict[str, Any],
    ) -> dict[str, bytes]:
        extracted: dict[str, bytes] = {}
        for entry in self._manifest_file_entries(manifest):
            try:
                extracted[entry["key"]] = self._read_tar_member(archive, entry["archive_path"])
            except ValueError:
                extracted[entry["key"]] = b""
        return extracted

    def _extract_manifest_directory_members(
        self,
        extract_dir: Path,
        manifest: dict[str, Any],
    ) -> dict[str, bytes]:
        extracted: dict[str, bytes] = {}
        for entry in self._manifest_file_entries(manifest):
            member_path = extract_dir / Path(entry["archive_path"])
            extracted[entry["key"]] = member_path.read_bytes() if member_path.exists() else b""
        return extracted

    def _manifest_file_entries(self, manifest: dict[str, Any]) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for index, raw_entry in enumerate(manifest.get("files", [])):
            if not isinstance(raw_entry, dict):
                continue
            archive_path = str(raw_entry.get("archive_path") or "").strip()
            if not archive_path:
                continue
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
        extracted_members: dict[str, bytes],
        target_path: Path,
        restored_paths: list[str],
    ) -> None:
        group_entry = group_entries.get(group_key)
        if not self._manifest_group_selected(group_entry):
            return
        member_entries = self._group_members(manifest, group_key)
        if member_entries:
            member_key = member_entries[0]["key"]
            if member_key not in extracted_members:
                raise ValueError(f"Backup bundle is missing the selected {group_key} member.")
            self._write_bytes_atomic(target_path, extracted_members[member_key])
            restored_paths.append(str(target_path))
            return
        if self._manifest_group_present(group_entry):
            raise ValueError(f"Backup bundle is missing the selected {group_key} member.")
        self._delete_if_exists(target_path)

    def _restore_directory_group(
        self,
        group_key: str,
        manifest: dict[str, Any],
        group_entries: dict[str, dict[str, Any]],
        extracted_members: dict[str, bytes],
        target_dir: Path,
        restored_paths: list[str],
    ) -> None:
        group_entry = group_entries.get(group_key)
        if not self._manifest_group_selected(group_entry):
            return
        member_entries = self._group_members(manifest, group_key)
        if not member_entries:
            if self._manifest_group_present(group_entry):
                raise ValueError(f"Backup bundle is missing the selected {group_key} directory members.")
            self._remove_tree_if_exists(target_dir)
            return

        self._remove_tree_if_exists(target_dir)
        for entry in member_entries:
            relative_path = self._directory_member_relative_path(group_key, entry["archive_path"])
            target_path = target_dir / relative_path
            member_key = entry["key"]
            if member_key not in extracted_members:
                raise ValueError(f"Backup bundle is missing the selected {group_key} member {entry['archive_path']}.")
            self._write_bytes_atomic(target_path, extracted_members[member_key])
            restored_paths.append(str(target_path))

    @staticmethod
    def _directory_member_relative_path(group_key: str, archive_path: str) -> Path:
        archive_root = str(BACKUP_GROUP_METADATA[group_key]["archive_root"]).strip("/")
        if archive_root and archive_path.startswith(f"{archive_root}/"):
            relative_text = archive_path[len(archive_root) + 1 :]
        else:
            relative_text = archive_path
        relative_path = Path(relative_text)
        if any(part in {"..", ""} for part in relative_path.parts):
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

    @staticmethod
    def _decompress_tar_archive(archive_bytes: bytes, packaging: ArchivePackaging) -> bytes:
        if packaging == "tar.gz":
            try:
                return gzip.decompress(archive_bytes)
            except OSError as exc:
                raise ValueError("Backup bundle tar.gz archive is corrupted.") from exc
        if packaging == "tar.zst":
            if zstd is None:
                raise ValueError("tar.zst import requires the optional 'zstandard' dependency.")
            try:
                return zstd.ZstdDecompressor().decompress(archive_bytes)
            except zstd.ZstdError as exc:
                raise ValueError("Backup bundle tar.zst archive is corrupted.") from exc
        raise ValueError(f"Unsupported tar archive packaging '{packaging}'.")

    def _run_7z_command(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [SEVEN_ZIP_BINARY, *args],
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SEVEN_ZIP_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                "Portable 7z backup support requires the '7z' command inside the container image."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Portable 7z backup operation timed out.") from exc

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
    def _delete_if_exists(target_path: Path) -> None:
        if target_path.exists():
            target_path.unlink()

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

    def _build_scrubbed_history_snapshot(self, snapshot_bytes: bytes, scrubber: DebugScrubber | None) -> bytes:
        if scrubber is None or not snapshot_bytes:
            return snapshot_bytes
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "history-scrub.sqlite3"
            temp_path.write_bytes(snapshot_bytes)
            connection = sqlite3.connect(temp_path)
            try:
                connection.row_factory = sqlite3.Row
                slot_state_rules: dict[str, Any] = {}
                slot_event_rules: dict[str, Any] = {}
                metric_sample_rules: dict[str, Any] = {}
                if scrubber.scrub_disk_identifiers:
                    slot_state_rules.update(
                        {
                            "device_name": scrubber.alias_device_name,
                            "serial": lambda value: scrubber.alias_identifier("serial", value),
                            "gptid": lambda value: scrubber.alias_identifier("gptid", value),
                            "persistent_id_label": lambda value: scrubber.alias_identifier("persistent_id", value),
                            "disk_identity_key": lambda value: scrubber.alias_identifier("disk_identity_key", value),
                            "logical_unit_id": lambda value: scrubber.alias_identifier("logical_unit_id", value),
                            "sas_address": lambda value: scrubber.alias_identifier("sas_address", value),
                            "multipath_device": scrubber.alias_device_name,
                            "multipath_lunid": lambda value: scrubber.alias_identifier("multipath_lunid", value),
                        }
                    )
                    slot_event_rules.update(
                        {
                            "device_name": scrubber.alias_device_name,
                            "serial": lambda value: scrubber.alias_identifier("serial", value),
                            "gptid": lambda value: scrubber.alias_identifier("gptid", value),
                            "persistent_id_label": lambda value: scrubber.alias_identifier("persistent_id", value),
                            "disk_identity_key": lambda value: scrubber.alias_identifier("disk_identity_key", value),
                            "logical_unit_id": lambda value: scrubber.alias_identifier("logical_unit_id", value),
                            "sas_address": lambda value: scrubber.alias_identifier("sas_address", value),
                        }
                    )
                    metric_sample_rules.update(
                        {
                            "device_name": scrubber.alias_device_name,
                            "serial": lambda value: scrubber.alias_identifier("serial", value),
                            "gptid": lambda value: scrubber.alias_identifier("gptid", value),
                            "persistent_id_label": lambda value: scrubber.alias_identifier("persistent_id", value),
                            "disk_identity_key": lambda value: scrubber.alias_identifier("disk_identity_key", value),
                            "logical_unit_id": lambda value: scrubber.alias_identifier("logical_unit_id", value),
                            "sas_address": lambda value: scrubber.alias_identifier("sas_address", value),
                        }
                    )
                if scrubber.scrub_secrets or scrubber.scrub_disk_identifiers:
                    slot_event_rules["details_json"] = scrubber.scrub_json_text
                if slot_state_rules:
                    self._scrub_history_table(connection, "slot_state_current", slot_state_rules)
                if slot_event_rules:
                    self._scrub_history_table(connection, "slot_events", slot_event_rules)
                if metric_sample_rules:
                    self._scrub_history_table(connection, "metric_samples", metric_sample_rules)
                connection.commit()
            finally:
                connection.close()
            return temp_path.read_bytes()

    @staticmethod
    def _scrub_history_table(
        connection: sqlite3.Connection,
        table_name: str,
        scrubbing_rules: dict[str, Any],
    ) -> None:
        rows = connection.execute(f"SELECT rowid AS _rowid_, * FROM {table_name}").fetchall()
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
