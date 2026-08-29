from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

_ARCHIVE_PREFIX = "jbod-scheduled-backup-"
_ARCHIVE_SUFFIX = ".tar.zst.enc"
_ARCHIVE_NAME = re.compile(
    r"^jbod-scheduled-backup-(?P<timestamp>[0-9]{8}T[0-9]{6}Z)-(?P<nonce>[0-9a-f]{8})\.tar\.zst\.enc$"
)
_MAX_PASSPHRASE_BYTES = 512
_MAX_STATUS_BYTES = 64 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_STATUS_SCHEMA_VERSION = 1
_STATUS_FIELDS = {
    "schema_version",
    "enabled",
    "included_groups",
    "success_count",
    "failure_count",
    "last_attempt_at",
    "last_success_at",
    "last_failure_at",
    "last_size_bytes",
    "last_sha256",
    "last_artifact_name",
    "last_absent_groups",
    "last_retention_removed",
    "last_error_code",
}


def validate_scheduled_backup_status(
    payload: dict[str, Any],
    *,
    expected_groups: tuple[str, ...] | None = None,
) -> None:
    if set(payload) != _STATUS_FIELDS:
        raise ValueError("Scheduled backup status is invalid.")
    if payload.get("schema_version") != _STATUS_SCHEMA_VERSION or payload.get("enabled") is not True:
        raise ValueError("Scheduled backup status is invalid.")
    included_groups = payload.get("included_groups")
    if (
        not isinstance(included_groups, list)
        or not included_groups
        or any(not isinstance(group, str) or not group for group in included_groups)
        or len(included_groups) != len(set(included_groups))
        or (expected_groups is not None and included_groups != list(expected_groups))
    ):
        raise ValueError("Scheduled backup status is invalid.")
    for key in ("success_count", "failure_count", "last_retention_removed"):
        value = payload.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("Scheduled backup status is invalid.")
    size_bytes = payload.get("last_size_bytes")
    if size_bytes is not None and (type(size_bytes) is not int or size_bytes < 0):
        raise ValueError("Scheduled backup status is invalid.")
    for key in ("last_attempt_at", "last_success_at", "last_failure_at"):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("Scheduled backup status is invalid.")  # noqa: TRY004
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Scheduled backup status is invalid.") from exc
        if parsed.tzinfo is None:
            raise ValueError("Scheduled backup status is invalid.")
    digest = payload.get("last_sha256")
    if digest is not None and (
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("Scheduled backup status is invalid.")
    artifact_name = payload.get("last_artifact_name")
    if artifact_name is not None and (
        not isinstance(artifact_name, str) or _ARCHIVE_NAME.fullmatch(artifact_name) is None
    ):
        raise ValueError("Scheduled backup status is invalid.")
    absent_groups = payload.get("last_absent_groups")
    if (
        not isinstance(absent_groups, list)
        or any(not isinstance(group, str) for group in absent_groups)
        or len(absent_groups) != len(set(absent_groups))
        or not set(absent_groups).issubset(included_groups)
    ):
        raise ValueError("Scheduled backup status is invalid.")
    error_code = payload.get("last_error_code")
    if error_code is not None and (
        not isinstance(error_code, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error_code) is None
    ):
        raise ValueError("Scheduled backup status is invalid.")


class ScheduledBackupSettings(BaseModel):
    enabled: bool = False
    destination_dir: str | None = None
    status_file: str | None = None
    retention_count: int = 0
    included_groups: list[str] = Field(default_factory=list)
    passphrase_file: str | None = None

    @model_validator(mode="after")
    def validate_enabled_settings(self) -> ScheduledBackupSettings:
        if not self.enabled:
            return self
        if not str(self.destination_dir or "").strip():
            raise ValueError("Scheduled backups require an explicit destination directory.")
        if not str(self.status_file or "").strip():
            raise ValueError("Scheduled backups require an explicit status file.")
        if self.retention_count <= 0:
            raise ValueError("Scheduled backups require a positive retention count.")
        groups = [str(group).strip() for group in self.included_groups if str(group).strip()]
        if not groups:
            raise ValueError("Scheduled backups require explicit included groups.")
        if len(groups) != len(set(groups)):
            raise ValueError("Scheduled backup included groups must be unique.")
        if not str(self.passphrase_file or "").strip():
            raise ValueError("Scheduled backups require an explicit passphrase file reference.")
        self.destination_dir = str(self.destination_dir).strip()
        self.status_file = str(self.status_file).strip()
        self.included_groups = groups
        self.passphrase_file = str(self.passphrase_file).strip()
        return self

    @classmethod
    def from_environment(cls) -> ScheduledBackupSettings:
        enabled = str(os.getenv("SCHEDULED_BACKUP_ENABLED", "false")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        raw_groups = str(os.getenv("SCHEDULED_BACKUP_INCLUDED_GROUPS_JSON", "[]"))
        try:
            groups = json.loads(raw_groups)
        except json.JSONDecodeError as exc:
            raise ValueError("SCHEDULED_BACKUP_INCLUDED_GROUPS_JSON must be valid JSON.") from exc
        if not isinstance(groups, list):
            raise ValueError(  # noqa: TRY004 - environment values are configuration text
                "SCHEDULED_BACKUP_INCLUDED_GROUPS_JSON must be a JSON array."
            )
        raw_retention = str(os.getenv("SCHEDULED_BACKUP_RETENTION_COUNT", "0")).strip()
        try:
            retention_count = int(raw_retention)
        except ValueError as exc:
            raise ValueError("SCHEDULED_BACKUP_RETENTION_COUNT must be an integer.") from exc
        return cls(
            enabled=enabled,
            destination_dir=os.getenv("SCHEDULED_BACKUP_DIR"),
            status_file=os.getenv("SCHEDULED_BACKUP_STATUS_FILE"),
            retention_count=retention_count,
            included_groups=groups,
            passphrase_file=os.getenv("SCHEDULED_BACKUP_PASSPHRASE_FILE"),
        )


class ScheduledBackupRunner:
    def __init__(
        self,
        backup_service: Any,
        *,
        destination_dir: str | Path,
        status_file: str | Path,
        passphrase_file: str | Path,
        included_groups: list[str] | tuple[str, ...],
        retention_count: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.backup_service = backup_service
        self.destination_dir = Path(destination_dir)
        self.status_file = Path(status_file)
        self.passphrase_file = Path(passphrase_file)
        self.included_groups = tuple(included_groups)
        self.retention_count = int(retention_count)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _ensure_private_directory(path: Path, *, label: str) -> None:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ValueError(f"Scheduled backup {label} must be a private directory.")

    def _ensure_directories(self) -> None:
        self._ensure_private_directory(self.destination_dir, label="destination")
        self._ensure_private_directory(self.status_file.parent, label="status directory")

    def _read_passphrase(self) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.passphrase_file, flags)
        except OSError as exc:
            raise ValueError("Scheduled backup passphrase must be a private regular file.") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError("Scheduled backup passphrase must be a private regular file.")
            content = os.read(descriptor, _MAX_PASSPHRASE_BYTES + 1)
            if len(content) > _MAX_PASSPHRASE_BYTES or os.read(descriptor, 1):
                raise ValueError("Scheduled backup passphrase file exceeds its size limit.")
        finally:
            os.close(descriptor)
        try:
            passphrase = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Scheduled backup passphrase file must contain UTF-8 text.") from exc
        if passphrase.endswith("\r\n"):
            passphrase = passphrase[:-2]
        elif passphrase.endswith("\n"):
            passphrase = passphrase[:-1]
        if "\x00" in passphrase:
            raise ValueError("Scheduled backup passphrase contains invalid characters.")
        if "\r" in passphrase or "\n" in passphrase:
            raise ValueError("Scheduled backup passphrase has ambiguous newline content.")
        if not passphrase:
            raise ValueError("Scheduled backup passphrase file must not be empty.")
        return passphrase

    def _validate_inputs(self) -> str:
        if self.retention_count <= 0:
            raise ValueError("Scheduled backup retention count must be positive.")
        if not self.included_groups or len(self.included_groups) != len(set(self.included_groups)):
            raise ValueError("Scheduled backup included groups must be explicit and unique.")
        passphrase = self._read_passphrase()
        self.backup_service.validate_scheduled_backup_scope(list(self.included_groups))
        return passphrase

    def validate_configuration(self) -> str:
        self._ensure_directories()
        return self._validate_inputs()

    @contextmanager
    def _destination_lock(self) -> Iterator[None]:
        lock_path = self.destination_dir / ".scheduled-backup.lock"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("Scheduled backup lock could not be opened.") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RuntimeError("Scheduled backup lock is not private.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Scheduled backup is already running.") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _default_status(self) -> dict[str, Any]:
        return {
            "schema_version": _STATUS_SCHEMA_VERSION,
            "enabled": True,
            "included_groups": list(self.included_groups),
            "success_count": 0,
            "failure_count": 0,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_failure_at": None,
            "last_size_bytes": None,
            "last_sha256": None,
            "last_artifact_name": None,
            "last_absent_groups": [],
            "last_retention_removed": 0,
            "last_error_code": None,
        }

    def _validate_status(self, payload: dict[str, Any]) -> None:
        validate_scheduled_backup_status(payload, expected_groups=self.included_groups)

    def _read_status(self) -> dict[str, Any]:
        if not self.status_file.exists():
            return self._default_status()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.status_file, flags)
        except OSError as exc:
            raise ValueError("Scheduled backup status could not be read.") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_STATUS_BYTES:
                raise ValueError("Scheduled backup status is invalid.")
            content = os.read(descriptor, _MAX_STATUS_BYTES + 1)
        finally:
            os.close(descriptor)
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Scheduled backup status is invalid.") from exc
        if not isinstance(payload, dict):
            raise ValueError(  # noqa: TRY004 - keep all status rejections generic
                "Scheduled backup status is invalid."
            )
        self._validate_status(payload)
        return payload

    def _write_status(self, status: dict[str, Any]) -> None:
        content = (json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(content) > _MAX_STATUS_BYTES:
            raise ValueError("Scheduled backup status exceeds its size limit.")
        temporary = self.status_file.with_name(f".{self.status_file.name}.{uuid.uuid4().hex}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.status_file)
            directory_descriptor = os.open(
                self.status_file.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _owned_archive(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        return stat.S_ISREG(metadata.st_mode) and _ARCHIVE_NAME.fullmatch(path.name) is not None

    def _apply_retention(self) -> int:
        owned = sorted(
            (path for path in self.destination_dir.iterdir() if self._owned_archive(path)),
            key=lambda path: path.name,
            reverse=True,
        )
        removed = 0
        for path in owned[self.retention_count :]:
            path.unlink()
            removed += 1
        return removed

    def _publish(
        self,
        source: Path,
        target: Path,
        *,
        passphrase: str,
    ) -> tuple[int, str, list[str]]:
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        descriptor: int | None = None
        identity_mismatch = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
                descriptor = None
                shutil.copyfileobj(input_stream, output_stream, length=_COPY_CHUNK_BYTES)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            preflight = self.backup_service.preflight_scheduled_bundle_file(
                temporary,
                passphrase=passphrase,
                expected_groups=list(self.included_groups),
            )
            source_metadata = temporary.lstat()
            os.link(temporary, target, follow_symlinks=False)
            target_descriptor: int | None = None
            try:
                target_descriptor = os.open(
                    target,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                target_metadata = os.fstat(target_descriptor)
                if (
                    not stat.S_ISREG(target_metadata.st_mode)
                    or (target_metadata.st_dev, target_metadata.st_ino)
                    != (source_metadata.st_dev, source_metadata.st_ino)
                ):
                    identity_mismatch = True
                    raise RuntimeError("Scheduled backup publication identity changed.")
                digest = hashlib.sha256()
                while chunk := os.read(target_descriptor, _COPY_CHUNK_BYTES):
                    digest.update(chunk)
                size_bytes = target_metadata.st_size

                directory_descriptor = os.open(
                    self.destination_dir,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)

                linked_metadata = target.lstat()
                descriptor_metadata = os.fstat(target_descriptor)
                if (
                    not stat.S_ISREG(linked_metadata.st_mode)
                    or (linked_metadata.st_dev, linked_metadata.st_ino)
                    != (source_metadata.st_dev, source_metadata.st_ino)
                    or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
                    != (source_metadata.st_dev, source_metadata.st_ino)
                    or descriptor_metadata.st_nlink != 2
                ):
                    identity_mismatch = True
                    raise RuntimeError("Scheduled backup publication identity changed.")

                temporary.unlink()
                directory_descriptor = os.open(
                    self.destination_dir,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)

                published_metadata = target.lstat()
                descriptor_metadata = os.fstat(target_descriptor)
                if (
                    not stat.S_ISREG(published_metadata.st_mode)
                    or (published_metadata.st_dev, published_metadata.st_ino)
                    != (source_metadata.st_dev, source_metadata.st_ino)
                    or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
                    != (source_metadata.st_dev, source_metadata.st_ino)
                    or descriptor_metadata.st_nlink != 1
                ):
                    identity_mismatch = True
                    raise RuntimeError("Scheduled backup publication identity changed.")
            except OSError as exc:
                identity_mismatch = True
                raise RuntimeError("Scheduled backup publication identity changed.") from exc
            finally:
                if target_descriptor is not None:
                    os.close(target_descriptor)
            absent_groups = preflight.get("absent_groups", []) if isinstance(preflight, dict) else []
            return size_bytes, digest.hexdigest(), list(absent_groups)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not identity_mismatch:
                temporary.unlink(missing_ok=True)

    def run_once(self) -> dict[str, Any]:
        self._ensure_directories()
        attempted_at = self._now()
        artifact = None
        with self._destination_lock():
            status = self._read_status()
            status["last_attempt_at"] = attempted_at.isoformat()
            try:
                passphrase = self._validate_inputs()
                artifact = self.backup_service.export_scheduled_bundle_to_file(
                    passphrase=passphrase,
                    included_paths=list(self.included_groups),
                )
                filename = (
                    f"{_ARCHIVE_PREFIX}{attempted_at.strftime('%Y%m%dT%H%M%SZ')}-"
                    f"{uuid.uuid4().hex[:8]}{_ARCHIVE_SUFFIX}"
                )
                size_bytes, digest, absent_groups = self._publish(
                    artifact.path,
                    self.destination_dir / filename,
                    passphrase=passphrase,
                )
                retention_removed = self._apply_retention()
                status.update(
                    {
                        "success_count": int(status.get("success_count") or 0) + 1,
                        "last_success_at": attempted_at.isoformat(),
                        "last_size_bytes": size_bytes,
                        "last_sha256": digest,
                        "last_artifact_name": filename,
                        "last_absent_groups": absent_groups,
                        "last_retention_removed": retention_removed,
                        "last_error_code": None,
                    }
                )
                self._write_status(status)
                return status
            except Exception as exc:
                status.update(
                    {
                        "failure_count": int(status.get("failure_count") or 0) + 1,
                        "last_failure_at": attempted_at.isoformat(),
                        "last_error_code": type(exc).__name__,
                    }
                )
                self._write_status(status)
                raise
            finally:
                if artifact is not None:
                    artifact.cleanup()
