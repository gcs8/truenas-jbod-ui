from __future__ import annotations

import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - exercised in runtime validation instead.
    zstd = None

from app import __version__
from app.config import Settings, get_settings
from history_service.config import HistorySettings
from history_service.store import HistoryStore


BUNDLE_SCHEMA_VERSION = 1
BUNDLE_FORMAT = "truenas-jbod-ui-backup"

CONFIG_FILE_KEY = "config_file"
PROFILE_FILE_KEY = "profile_file"
MAPPING_FILE_KEY = "mapping_file"
SLOT_DETAIL_FILE_KEY = "slot_detail_file"
HISTORY_DB_KEY = "history_db"

EXPORT_ARCHIVE_PATHS: dict[str, str] = {
    CONFIG_FILE_KEY: "config/config.yaml",
    PROFILE_FILE_KEY: "config/profiles.yaml",
    MAPPING_FILE_KEY: "data/slot_mappings.json",
    SLOT_DETAIL_FILE_KEY: "data/slot_detail_cache.json",
    HISTORY_DB_KEY: "history/history.sqlite3",
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
    archive_path: str
    source_path: str | None
    present: bool
    content: bytes | None


@dataclass(slots=True)
class BackupArtifact:
    filename: str
    content: bytes
    media_type: str
    manifest: dict[str, Any]


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
    ) -> BackupArtifact:
        app_settings = self._load_app_settings()
        exported_at = datetime.now(timezone.utc)
        history_snapshot_bytes = self._build_history_snapshot()
        requested_packaging = self._normalize_packaging(packaging)
        if encrypt and not passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        normalized_packaging: ArchivePackaging = "7z" if encrypt else requested_packaging
        bundle_members = self._collect_bundle_members(app_settings, history_snapshot_bytes)

        manifest = self._build_manifest(
            app_settings=app_settings,
            exported_at=exported_at,
            packaging=normalized_packaging,
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

        file_entries = {
            str(entry.get("key")): entry
            for entry in manifest.get("files", [])
            if isinstance(entry, dict) and entry.get("key")
        }

        restored_paths: list[str] = []
        app_settings = self._load_app_settings()

        config_entry = file_entries.get(CONFIG_FILE_KEY)
        config_target = Path(app_settings.config_file)
        if config_entry and config_entry.get("present"):
            self._write_bytes_atomic(config_target, extracted[CONFIG_FILE_KEY])
            restored_paths.append(str(config_target))
        elif config_entry:
            self._delete_if_exists(config_target)

        imported_settings = self._load_app_settings()
        restore_targets = {
            PROFILE_FILE_KEY: Path(imported_settings.paths.profile_file),
            MAPPING_FILE_KEY: Path(imported_settings.paths.mapping_file),
            SLOT_DETAIL_FILE_KEY: Path(imported_settings.paths.slot_detail_cache_file),
        }

        for key, target_path in restore_targets.items():
            file_entry = file_entries.get(key)
            if file_entry and file_entry.get("present"):
                self._write_bytes_atomic(target_path, extracted[key])
                restored_paths.append(str(target_path))
            elif file_entry:
                self._delete_if_exists(target_path)

        history_restored = False
        history_entry = file_entries.get(HISTORY_DB_KEY)
        if history_entry and history_entry.get("present"):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_history_path = Path(temp_dir) / "history-import.sqlite3"
                temp_history_path.write_bytes(extracted[HISTORY_DB_KEY])
                self.store.restore_backup(temp_history_path)
            restored_paths.append(str(self.store.file_path))
            history_restored = True

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

    def _build_manifest(
        self,
        *,
        app_settings: Settings,
        exported_at: datetime,
        packaging: ArchivePackaging,
        bundle_members: list[BundleMember],
    ) -> dict[str, Any]:
        file_specs = self._collect_file_specs(bundle_members)
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
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
            "files": file_specs,
        }

    def _collect_file_specs(self, bundle_members: list[BundleMember]) -> list[dict[str, Any]]:
        manifest_files: list[dict[str, Any]] = []
        for member in bundle_members:
            content_bytes = member.content or b""
            manifest_files.append(
                {
                    "key": member.key,
                    "archive_path": member.archive_path,
                    "source_path": member.source_path,
                    "present": member.present,
                    "size_bytes": len(content_bytes),
                    "sha256": hashlib.sha256(content_bytes).hexdigest() if member.present else None,
                }
            )
        return manifest_files

    def _collect_bundle_members(
        self,
        app_settings: Settings,
        history_snapshot_bytes: bytes,
    ) -> list[BundleMember]:
        file_specs: list[tuple[str, Path | None, bytes | None]] = [
            (CONFIG_FILE_KEY, Path(app_settings.config_file), None),
            (PROFILE_FILE_KEY, Path(app_settings.paths.profile_file), None),
            (MAPPING_FILE_KEY, Path(app_settings.paths.mapping_file), None),
            (SLOT_DETAIL_FILE_KEY, Path(app_settings.paths.slot_detail_cache_file), None),
            (HISTORY_DB_KEY, None, history_snapshot_bytes),
        ]
        bundle_members: list[BundleMember] = []
        for key, path_value, inline_bytes in file_specs:
            if inline_bytes is not None:
                present = bool(inline_bytes)
                content = inline_bytes if present else None
                source_path = str(self.store.file_path) if key == HISTORY_DB_KEY else None
            elif path_value is not None and path_value.exists():
                present = True
                content = path_value.read_bytes()
                source_path = str(path_value)
            else:
                present = False
                content = None
                source_path = str(path_value) if path_value is not None else None

            bundle_members.append(
                BundleMember(
                    key=key,
                    archive_path=EXPORT_ARCHIVE_PATHS[key],
                    source_path=source_path,
                    present=present,
                    content=content,
                )
            )
        return bundle_members

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
                    extracted = self._extract_known_zip_members(archive)
            except zipfile.BadZipFile as exc:
                raise ValueError("Backup bundle ZIP archive is corrupted.") from exc
            return manifest, extracted, packaging, {"encrypted": False}

        tar_bytes = self._decompress_tar_archive(archive_bytes, packaging)
        try:
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
                manifest = self._load_manifest(self._read_tar_member(archive, "manifest.json"))
                extracted = self._extract_known_tar_members(archive)
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
            extracted: dict[str, bytes] = {}
            for key, archive_member_path in EXPORT_ARCHIVE_PATHS.items():
                member_path = extract_dir / Path(archive_member_path)
                extracted[key] = member_path.read_bytes() if member_path.exists() else b""
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

    @staticmethod
    def _extract_known_zip_members(archive: zipfile.ZipFile) -> dict[str, bytes]:
        extracted: dict[str, bytes] = {}
        for key, archive_path in EXPORT_ARCHIVE_PATHS.items():
            try:
                extracted[key] = archive.read(archive_path)
            except KeyError:
                extracted[key] = b""
        return extracted

    @classmethod
    def _extract_known_tar_members(cls, archive: tarfile.TarFile) -> dict[str, bytes]:
        extracted: dict[str, bytes] = {}
        for key, archive_path in EXPORT_ARCHIVE_PATHS.items():
            try:
                extracted[key] = cls._read_tar_member(archive, archive_path)
            except ValueError:
                extracted[key] = b""
        return extracted

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
