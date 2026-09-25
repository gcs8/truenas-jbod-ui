"""Settings for one backup archive target.

A target is a place the backup sidecar copies finished backup files to:
a local or mounted directory, an FTP/FTPS server, an SFTP server, an SMB
share, an NFS export mounted only for the duration of a job, or an S3 (or
S3-compatible) bucket. Every target writes only below its configured
app-owned ``root`` directory or key prefix.

Credentials never live in these settings. Each secret is named by a
``*_file`` path that points at a private regular file (Docker/Compose secret
style); the transport reads the file when it opens the target and keeps the
value in memory only for that block.

Nothing reads these settings from the environment yet; the integration change
wires them up. Until then the archive tier is disabled.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Any

PROVIDERS = ("filesystem", "ftp", "sftp", "smb", "nfs", "s3")
MAX_SECRET_BYTES = 65_536

_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ROOT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9._ -]{0,254}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,253}$")
_MOUNT_OPTIONS_RE = re.compile(r"^[A-Za-z0-9_=.:/-]+(,[A-Za-z0-9_=.:/-]+)*$")
_SHARE_RE = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9._$ -]{0,79}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

DEFAULT_PORTS = {"ftp": 21, "sftp": 22, "smb": 445}


class ArchiveConfigError(ValueError):
    """A target setting is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class ArchiveTargetSettings:
    """Configuration for one archive target. Holds secret file paths, never secrets."""

    target_id: str
    provider: str
    # App-owned directory (filesystem: absolute local path; ftp/sftp/smb/nfs:
    # path on the server/share/export; s3: key prefix). Never the bare root.
    root: str
    hostname: str = ""
    port: int | None = None
    username: str = ""
    password_file: str = ""
    timeout_seconds: float = 30.0
    # ftp: explicit FTPS (AUTH TLS, then PROT P). Plain FTP is allowed.
    use_tls: bool = False
    # sftp: host-key verification. known_hosts_path is required; an unknown
    # key fails unless trust_on_first_use is set for this target.
    known_hosts_path: str = ""
    trust_on_first_use: bool = False
    private_key_file: str = ""
    private_key_passphrase_file: str = ""
    # smb
    share: str = ""
    domain: str = ""
    smb_encrypt: bool = False
    # nfs
    export_path: str = ""
    mount_options: str = ""
    mount_parent: str = ""
    # s3
    bucket: str = ""
    region: str = ""
    endpoint_url: str = ""
    access_key_id_file: str = ""
    secret_access_key_file: str = ""

    def __post_init__(self) -> None:
        validate_settings(self)

    def __repr__(self) -> str:  # never render host credentials or paths in bulk logs
        return (
            f"ArchiveTargetSettings(target_id={self.target_id!r}, provider={self.provider!r}, "
            f"root={self.root!r}, hostname={self.hostname!r})"
        )

    @property
    def effective_port(self) -> int | None:
        return self.port or DEFAULT_PORTS.get(self.provider)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ArchiveTargetSettings:
        """Build settings from a plain mapping, rejecting unknown keys and inline secrets."""

        allowed = {field.name for field in fields(cls)}
        inline_secrets = {"password", "secret_access_key", "access_key_id", "private_key", "passphrase"}
        found_inline = sorted(inline_secrets & set(values))
        if found_inline:
            raise ArchiveConfigError(
                "Archive target credentials must come from *_file secret files, not inline values: "
                + ", ".join(found_inline)
            )
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ArchiveConfigError("Unknown archive target settings: " + ", ".join(unknown))
        return cls(**dict(values))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ArchiveConfigError(message)


def normalized_root_parts(root: str) -> tuple[str, ...]:
    """Split an app-owned root into validated segments (no '.', '..', or empty)."""

    text = str(root or "").strip()
    _require(bool(text), "Archive target root directory is required.")
    _require("\\" not in text and "\x00" not in text, "Archive target root must use '/' separators.")
    parts = tuple(part for part in PurePosixPath(text).parts if part not in {"/", ""})
    _require(bool(parts), "Archive target root must name an app-owned directory, not '/'.")
    for part in parts:
        _require(part not in {".", ".."}, "Archive target root must not contain '.' or '..' segments.")
        _require(bool(_ROOT_SEGMENT_RE.match(part)), f"Archive target root segment is not allowed: {part!r}")
    return parts


def _directory_anchors(root: str | os.PathLike[str], *, label: str) -> list[tuple[tuple[int, int], tuple[str, ...]]]:
    """Describe a path by existing directory identities plus unresolved suffixes.

    ``realpath`` resolves existing symlinks without requiring the final path to
    exist. Each resolved component is then inspected with ``lstat`` so a path
    swap cannot make this check follow a new symlink. Directory device/inode
    identities let bind-mounted aliases compare equal where the platform
    exposes that fact. A missing suffix is recorded but never created or opened.
    """

    text = os.fspath(root)
    _require(bool(text) and os.path.isabs(text), f"{label} must be an absolute path.")
    try:
        resolved = Path(os.path.realpath(text))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArchiveConfigError(f"{label} could not be resolved safely.") from exc
    parts = resolved.parts
    anchors: list[tuple[tuple[int, int], tuple[str, ...]]] = []
    current = Path(parts[0])
    for index in range(len(parts)):
        if index:
            current /= parts[index]
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ArchiveConfigError(f"{label} could not be inspected safely.") from exc
        if stat.S_ISLNK(metadata.st_mode):
            # ``realpath`` returned this as a plain component but it changed
            # before inspection. Refuse the unstable result instead of following it.
            raise ArchiveConfigError(f"{label} changed while it was being resolved.")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ArchiveConfigError(f"{label} must resolve through directories only.")
        anchors.append(((metadata.st_dev, metadata.st_ino), tuple(parts[index + 1 :])))
    return anchors


def _is_path_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and right[: len(left)] == left


def filesystem_roots_overlap(
    local_archive_root: str | os.PathLike[str],
    target_root: str | os.PathLike[str],
) -> bool:
    """Return whether two roots are the same physical tree or contain each other.

    Existing symlinks are resolved, missing tails remain lexical, and matching
    directory identities detect bind-visible aliases. Inspection failures raise
    :class:`ArchiveConfigError` so callers fail closed rather than accepting an
    uncertain target.
    """

    local_anchors = _directory_anchors(local_archive_root, label="Local backup archive root")
    target_anchors = _directory_anchors(target_root, label="Filesystem archive target root")
    for local_identity, local_suffix in local_anchors:
        for target_identity, target_suffix in target_anchors:
            if local_identity != target_identity:
                continue
            if _is_path_prefix(local_suffix, target_suffix) or _is_path_prefix(target_suffix, local_suffix):
                return True
    return False


def validate_settings(settings: ArchiveTargetSettings) -> None:
    _require(bool(_TARGET_ID_RE.match(settings.target_id or "")), "Archive target id is invalid.")
    _require(settings.provider in PROVIDERS, f"Unsupported archive provider: {settings.provider!r}")
    normalized_root_parts(settings.root)
    _require(settings.timeout_seconds > 0, "Archive target timeout must be positive.")
    if settings.port is not None:
        _require(0 < int(settings.port) < 65536, "Archive target port is out of range.")
    provider = settings.provider
    if provider == "filesystem":
        _require(settings.root.startswith("/"), "Filesystem archive root must be an absolute path.")
    if provider in {"ftp", "sftp", "smb", "nfs"}:
        _require(bool(_HOST_RE.match(settings.hostname or "")), f"{provider} archive host is required.")
    if provider == "sftp":
        _require(bool(settings.username), "SFTP archive username is required.")
        _require(bool(settings.known_hosts_path), "SFTP archive requires known_hosts_path.")
        _require(
            bool(settings.password_file or settings.private_key_file),
            "SFTP archive requires password_file or private_key_file.",
        )
    if provider == "smb":
        _require(bool(_SHARE_RE.match(settings.share or "")), "SMB archive share is required.")
    if provider == "nfs":
        export_path = settings.export_path or ""
        _require(export_path.startswith("/"), "NFS archive export path must be absolute, such as /export/backups.")
        _require(
            ".." not in PurePosixPath(export_path).parts and not re.search(r"[\s,\x00]", export_path),
            "NFS archive export path is not allowed.",
        )
        if settings.mount_options:
            _require(
                bool(_MOUNT_OPTIONS_RE.match(settings.mount_options)),
                "NFS mount options must be a comma-separated list without spaces.",
            )
        if settings.mount_parent:
            _require(settings.mount_parent.startswith("/"), "NFS mount parent must be an absolute path.")
    if provider == "s3":
        _require(bool(_BUCKET_RE.match(settings.bucket or "")), "S3 archive bucket is required.")
        _require(
            bool(settings.access_key_id_file) and bool(settings.secret_access_key_file),
            "S3 archive requires access_key_id_file and secret_access_key_file.",
        )
        if settings.endpoint_url:
            _require(
                settings.endpoint_url.startswith(("https://", "http://")),
                "S3 endpoint URL must start with https:// or http://.",
            )


def read_secret_file(path: str, label: str) -> str:
    """Read one secret from a private regular file.

    Mirrors ``SCHEDULED_BACKUP_PASSPHRASE_FILE`` handling: O_NOFOLLOW, the file
    must be a regular file with no group/other permission bits, bounded size,
    UTF-8, one trailing newline stripped. Error messages never include content.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    message = f"Archive {label} must be a private regular file."
    try:
        descriptor = os.open(Path(path), flags)
    except OSError as exc:
        raise ArchiveConfigError(message) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ArchiveConfigError(message)
        content = os.read(descriptor, MAX_SECRET_BYTES + 1)
        if len(content) > MAX_SECRET_BYTES:
            raise ArchiveConfigError(f"Archive {label} file exceeds its size limit.")
    finally:
        os.close(descriptor)
    try:
        value = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveConfigError(f"Archive {label} file must contain UTF-8 text.") from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if "\x00" in value:
        raise ArchiveConfigError(f"Archive {label} contains invalid characters.")
    if not value:
        raise ArchiveConfigError(f"Archive {label} file must not be empty.")
    return value
