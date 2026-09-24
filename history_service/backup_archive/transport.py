"""Remote archive targets for finished backup files.

Adapted from switch-explorer's ``app/services/config_backup_archive.py`` (at
``816ef86``): the same six providers (filesystem, FTP/FTPS, SFTP via paramiko,
SMB via the smbprotocol direct client, NFS via a job-scoped mount, and S3 via
boto3), refactored to the shared ``ArchiveTarget`` interface and with the
defects found in review fixed (see #573 and gcs8/switch-explorer#179):

* NFS mounts on ``mkdtemp()`` and the mount point is only ``rmdir``'d after a
  confirmed unmount (retry, then ``umount -l``). Nothing here ever removes a
  directory tree, so a failed unmount cannot delete files on the export.
* SFTP verifies the server host key against a known_hosts file; an unknown key
  fails unless trust-on-first-use is explicitly enabled for that target.
* Uploads go to ``<name>.partial``, are verified, and are then renamed into
  place (S3: a single put or multipart upload, then verify).
* Every upload is streamed from disk and read back: size plus SHA-256 where the
  protocol allows it (FTP: size only; S3: stored SHA-256 metadata plus ETag, or
  a re-GET for small objects).
* Object names are validated and every target stays below its app-owned root.
* Credentials come from private ``*_file`` secret files only.
* "Ensure directory" loops only tolerate "already exists" and then confirm the
  path really is a directory.

Pruning is intentionally absent: the lifecycle module decides what to delete
from its catalog and calls ``delete()`` by exact name.

Nothing in the running application uses this module yet.
"""

from __future__ import annotations

import errno
import ftplib
import hashlib
import io
import logging
import os
import posixpath
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable

import paramiko

from history_service.backup_archive.settings import (
    ArchiveConfigError,
    ArchiveTargetSettings,
    normalized_root_parts,
    read_secret_file,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
PARTIAL_SUFFIX = ".partial"
MAX_NAME_LENGTH = 1024
MAX_NAME_DEPTH = 8
# S3: re-download objects up to this size to hash them; larger objects rely on
# the stored SHA-256 metadata plus the ETag computed for our fixed part size.
S3_REGET_LIMIT = 16 * 1024 * 1024
S3_MULTIPART_THRESHOLD = 64 * 1024 * 1024
S3_MULTIPART_CHUNKSIZE = 64 * 1024 * 1024
SFTP_PREFETCH_MAX_REQUESTS = 64
NFS_UMOUNT_ATTEMPTS = 3
NFS_UMOUNT_RETRY_DELAY_SECONDS = 2.0
MOUNT_COMMAND_TIMEOUT_SECONDS = 45
PROBE_CONTENT = b"truenas-jbod-ui archive destination test\n"

_NAME_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")


class ArchiveTransportError(RuntimeError):
    """A transfer or remote operation failed."""


class ArchiveVerificationError(ArchiveTransportError):
    """The readback check did not match what was sent."""


class NfsUnmountError(ArchiveTransportError):
    """The NFS export could not be confirmed unmounted; the mount point was left in place."""


class DependencyMissingError(ArchiveTransportError):
    """An optional client library for this provider is not installed."""


@dataclass(frozen=True, slots=True)
class RemoteObject:
    name: str
    size: int
    modified: datetime | None


@dataclass(frozen=True, slots=True)
class StoredObject:
    name: str
    size: int
    sha256: str
    verified: bool


@runtime_checkable
class ArchiveTarget(Protocol):
    provider: str

    @property
    def transport_encrypted(self) -> bool: ...

    def put(self, local_path: Path, name: str) -> StoredObject: ...

    def list(self, prefix: str = "") -> list[RemoteObject]: ...

    def delete(self, name: str) -> None: ...

    def test(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# Names and local streaming helpers
# --------------------------------------------------------------------------


def validate_object_name(name: str) -> tuple[str, ...]:
    """Return the '/'-separated segments of a safe relative object name.

    Rejects absolute names, backslashes, '.', '..', empty segments, control or
    unusual characters, and names ending in the reserved '.partial' suffix.
    """

    if not isinstance(name, str) or not name:
        raise ValueError("Archive object name is required.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError("Archive object name is too long.")
    if name.startswith("/") or "\\" in name or "\x00" in name:
        raise ValueError("Archive object name must be a relative '/'-separated path.")
    parts = tuple(name.split("/"))
    if len(parts) > MAX_NAME_DEPTH:
        raise ValueError("Archive object name is nested too deeply.")
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError("Archive object name must not contain empty, '.', or '..' segments.")
        if not _NAME_SEGMENT_RE.match(part):
            raise ValueError(f"Archive object name segment is not allowed: {part!r}")
    if name.endswith(PARTIAL_SUFFIX):
        raise ValueError("Archive object names must not end in the reserved '.partial' suffix.")
    return parts


def _validate_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    trimmed = prefix[:-1] if prefix.endswith("/") else prefix
    validate_object_name(trimmed)
    return prefix


def _is_listable_name(name: str) -> bool:
    try:
        validate_object_name(name)
    except ValueError:
        return False
    return True


@contextmanager
def _open_local_source(local_path: Path) -> Iterator[tuple[BinaryIO, int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(Path(local_path), flags)
    except OSError as exc:
        raise ArchiveTransportError("Archive source must be a readable regular file.") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ArchiveTransportError("Archive source must be a readable regular file.")
    handle = os.fdopen(descriptor, "rb")
    try:
        yield handle, metadata.st_size
    finally:
        handle.close()


class _HashingReader(io.RawIOBase):
    """File-like wrapper that hashes (and counts) bytes as a client library reads them."""

    def __init__(self, source: BinaryIO) -> None:
        super().__init__()
        self._source = source
        self.sha256 = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size if size is not None and size >= 0 else CHUNK_SIZE)
        if chunk:
            self.sha256.update(chunk)
            self.size += len(chunk)
        return chunk

    def readinto(self, buffer: Any) -> int:
        chunk = self.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _copy_stream(source: Any, write: Callable[[bytes], Any]) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(CHUNK_SIZE)
        if not chunk:
            break
        write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _hash_stream(source: Any) -> tuple[int, str]:
    return _copy_stream(source, lambda _chunk: None)


def _check_readback(label: str, expected_size: int, expected_sha: str, size: int, sha: str | None) -> None:
    if size != expected_size:
        raise ArchiveVerificationError(
            f"{label} readback size mismatch: sent {expected_size} bytes, found {size}."
        )
    if sha is not None and sha != expected_sha:
        raise ArchiveVerificationError(f"{label} readback SHA-256 does not match the bytes sent.")


def _probe_name() -> str:
    return f"archive-probe-{uuid.uuid4().hex}.tmp"


class _TargetBase:
    provider = ""

    @property
    def transport_encrypted(self) -> bool:
        return False

    def put(self, local_path: Path, name: str) -> StoredObject:  # pragma: no cover - abstract
        raise NotImplementedError

    def delete(self, name: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def test(self) -> dict[str, Any]:
        """Write, verify, and delete a small probe object below the target root."""

        started = time.monotonic()
        probe = _probe_name()
        try:
            with tempfile.TemporaryDirectory(prefix="archive-probe-") as scratch:
                local = Path(scratch) / "probe"
                local.write_bytes(PROBE_CONTENT)
                stored = self.put(local, probe)
            try:
                self.delete(probe)
            except Exception as exc:  # report the failed cleanup, do not mask it
                raise ArchiveTransportError(f"Probe written but could not be deleted: {exc}") from exc
            detail = f"{self.provider} archive destination is writable"
            detail += " and readback matched." if stored.verified else "; readback could not confirm the content."
            ok = True
        except Exception as exc:  # the probe reports any failure instead of raising
            ok = False
            detail = str(exc) or exc.__class__.__name__
        return {
            "ok": ok,
            "detail": detail,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "provider": self.provider,
            "transport_encrypted": self.transport_encrypted,
        }


# --------------------------------------------------------------------------
# Filesystem (also used for NFS once mounted)
# --------------------------------------------------------------------------


class LocalDirectoryTarget(_TargetBase):
    """A directory on a local or already-mounted filesystem."""

    def __init__(
        self,
        root: Path,
        *,
        provider: str = "filesystem",
        confine_to: Path | None = None,
        encrypted: bool = True,
    ) -> None:
        self.provider = provider
        self._encrypted = encrypted
        self._root = Path(root)
        self._confine_to = Path(confine_to) if confine_to is not None else None
        self._root_real: Path | None = None

    @property
    def transport_encrypted(self) -> bool:
        # A local directory never crosses the network; NFS reports its mount security.
        return self._encrypted

    def _ensure_root(self) -> Path:
        if self._root_real is not None:
            return self._root_real
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise ArchiveTransportError("Archive root is not a directory.")
        real = Path(os.path.realpath(self._root))
        if self._confine_to is not None:
            base = Path(os.path.realpath(self._confine_to))
            if real != base and base not in real.parents:
                raise ArchiveTransportError("Archive root resolves outside its mount.")
        self._root_real = real
        return real

    def _object_path(self, name: str, *, create_parents: bool) -> Path:
        parts = validate_object_name(name)
        root = self._ensure_root()
        current = root
        for part in parts[:-1]:
            current = current / part
            if create_parents:
                try:
                    os.mkdir(current, 0o750)
                except FileExistsError:
                    pass
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                if create_parents:
                    raise
                return root.joinpath(*parts)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ArchiveTransportError("Archive object parent is not a plain directory.")
        return root.joinpath(*parts)

    def put(self, local_path: Path, name: str) -> StoredObject:
        final = self._object_path(name, create_parents=True)
        partial = final.with_name(final.name + PARTIAL_SUFFIX)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        with _open_local_source(local_path) as (source, _size):
            descriptor = os.open(partial, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    size, sha = _copy_stream(source, target.write)
                    target.flush()
                    os.fsync(target.fileno())
                with open(partial, "rb") as readback:
                    back_size, back_sha = _hash_stream(readback)
                _check_readback(self.provider, size, sha, back_size, back_sha)
                os.replace(partial, final)
            except BaseException:
                try:
                    os.unlink(partial)
                except FileNotFoundError:
                    pass
                raise
        self._fsync_dir(final.parent)
        return StoredObject(name=name, size=size, sha256=sha, verified=True)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = _validate_prefix(prefix)
        root = self._ensure_root()
        results: list[RemoteObject] = []
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for filename in sorted(filenames):
                path = Path(directory) / filename
                relative = path.relative_to(root).as_posix()
                if not relative.startswith(prefix) or not _is_listable_name(relative):
                    continue
                metadata = os.lstat(path)
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                results.append(
                    RemoteObject(
                        name=relative,
                        size=metadata.st_size,
                        modified=datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc),
                    )
                )
        return results

    def delete(self, name: str) -> None:
        path = self._object_path(name, create_parents=False)
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode):
            raise ArchiveTransportError("Archive delete refuses to remove a directory.")
        os.unlink(path)


# --------------------------------------------------------------------------
# FTP / FTPS
# --------------------------------------------------------------------------


def _ftp_modified(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class FtpTarget(_TargetBase):
    provider = "ftp"

    def __init__(self, ftp: ftplib.FTP, root: str, *, encrypted: bool) -> None:
        self._ftp = ftp
        self._root = root
        self._encrypted = encrypted
        self._known_dirs: set[str] = set()

    @property
    def transport_encrypted(self) -> bool:
        return self._encrypted

    def _path(self, *parts: str) -> str:
        joined = posixpath.join(self._root, *parts) if parts else self._root
        return joined

    def _is_dir(self, path: str) -> bool:
        original = self._ftp.pwd()
        try:
            self._ftp.cwd(path)
        except ftplib.error_perm:
            return False
        self._ftp.cwd(original)
        return True

    def _ensure_dir(self, path: str) -> None:
        absolute = path.startswith("/")
        current = "/" if absolute else ""
        for part in [segment for segment in path.split("/") if segment]:
            current = posixpath.join(current, part) if current else part
            if current in self._known_dirs:
                continue
            try:
                self._ftp.mkd(current)
            except ftplib.error_perm as exc:
                # 550 usually means "exists"; anything else, or a path that is
                # not a directory afterwards, is a real error.
                if not self._is_dir(current):
                    raise ArchiveTransportError(f"FTP archive could not create directory {current!r}: {exc}") from exc
            self._known_dirs.add(current)

    def put(self, local_path: Path, name: str) -> StoredObject:
        parts = validate_object_name(name)
        final = self._path(*parts)
        partial = final + PARTIAL_SUFFIX
        self._ensure_dir(posixpath.dirname(final))
        try:
            with _open_local_source(local_path) as (source, _size):
                reader = _HashingReader(source)
                self._ftp.storbinary(f"STOR {partial}", reader, blocksize=CHUNK_SIZE)
            size, sha = reader.size, reader.sha256.hexdigest()
            self._ftp.voidcmd("TYPE I")
            remote_size = self._ftp.size(partial)
            _check_readback("FTP", size, sha, int(remote_size if remote_size is not None else -1), None)
            # Never delete an existing object to make room: if the server
            # refuses RNTO onto an existing name, the upload fails instead.
            self._ftp.rename(partial, final)
        except BaseException:
            try:
                self._ftp.delete(partial)
            except ftplib.all_errors:
                logger.warning("FTP archive could not remove an incomplete upload.")
            raise
        # FTP offers size only; SHA-256 is the hash of the bytes sent.
        return StoredObject(name=name, size=size, sha256=sha, verified=True)

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = _validate_prefix(prefix)
        results: list[RemoteObject] = []
        self._walk("", results)
        return [item for item in results if item.name.startswith(prefix)]

    def _walk(self, relative: str, results: list[RemoteObject]) -> None:
        path = self._path(relative) if relative else self._root
        try:
            entries = list(self._ftp.mlsd(path, facts=["type", "size", "modify"]))
        except ftplib.error_perm as exc:
            if not relative and not self._is_dir(path):
                return
            raise ArchiveTransportError(f"FTP archive listing failed (MLSD required): {exc}") from exc
        for entry_name, facts in sorted(entries):
            kind = (facts.get("type") or "").lower()
            if kind in {"cdir", "pdir"} or entry_name in {".", ".."}:
                continue
            child = f"{relative}/{entry_name}" if relative else entry_name
            if kind == "dir":
                self._walk(child, results)
            elif kind == "file" and _is_listable_name(child):
                results.append(
                    RemoteObject(
                        name=child,
                        size=int(facts.get("size") or 0),
                        modified=_ftp_modified(facts.get("modify")),
                    )
                )

    def delete(self, name: str) -> None:
        parts = validate_object_name(name)
        path = self._path(*parts)
        try:
            self._ftp.delete(path)
        except ftplib.error_perm as exc:
            # Only a listing of the parent that succeeds and lacks the name
            # proves the object is gone; anything ambiguous propagates.
            parent, leaf = posixpath.split(path)
            try:
                present = any(entry == leaf for entry, _facts in self._ftp.mlsd(parent, facts=["type"]))
            except ftplib.all_errors as list_error:
                raise ArchiveTransportError(f"FTP archive delete failed: {exc}") from list_error
            if present:
                raise ArchiveTransportError(f"FTP archive delete failed: {exc}") from exc


@contextmanager
def _open_ftp(settings: ArchiveTargetSettings) -> Iterator[FtpTarget]:
    password = read_secret_file(settings.password_file, "FTP password") if settings.password_file else ""
    if settings.use_tls:
        ftp: ftplib.FTP = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=settings.timeout_seconds)
    else:
        ftp = ftplib.FTP(timeout=settings.timeout_seconds)
    try:
        ftp.connect(settings.hostname, settings.effective_port or 21, timeout=settings.timeout_seconds)
        ftp.login(settings.username or "anonymous", password)
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        root = "/".join(normalized_root_parts(settings.root))
        if settings.root.startswith("/"):
            root = "/" + root
        target = FtpTarget(ftp, root, encrypted=settings.use_tls)
        target._ensure_dir(root)
        yield target
    finally:
        try:
            ftp.quit()
        except (*ftplib.all_errors, AttributeError):
            ftp.close()


# --------------------------------------------------------------------------
# SFTP
# --------------------------------------------------------------------------


def _load_private_key(settings: ArchiveTargetSettings) -> paramiko.PKey | None:
    if not settings.private_key_file:
        return None
    key_text = read_secret_file(settings.private_key_file, "SFTP private key")
    passphrase = (
        read_secret_file(settings.private_key_passphrase_file, "SFTP private key passphrase")
        if settings.private_key_passphrase_file
        else None
    )
    for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_class.from_private_key(io.StringIO(key_text), password=passphrase)
        except (paramiko.SSHException, ValueError):
            continue
    raise ArchiveConfigError("SFTP private key file is not a supported unencrypted or passphrase-protected key.")


def _sftp_host_key_policy(client: paramiko.SSHClient, settings: ArchiveTargetSettings) -> None:
    known_hosts = Path(settings.known_hosts_path)
    if settings.trust_on_first_use:
        from app.services.ssh_probe import AutoPinHostKeyPolicy

        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts.exists():
            known_hosts.touch(mode=0o600)
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(AutoPinHostKeyPolicy(str(known_hosts)))
        return
    if not known_hosts.is_file():
        raise ArchiveConfigError(
            "SFTP archive known_hosts file does not exist; add the server key or enable "
            "trust-on-first-use for this target."
        )
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())


def _is_missing(exc: OSError) -> bool:
    return isinstance(exc, FileNotFoundError) or getattr(exc, "errno", None) == errno.ENOENT


class SftpTarget(_TargetBase):
    provider = "sftp"

    def __init__(self, sftp: paramiko.SFTPClient, root: str) -> None:
        self._sftp = sftp
        self._root = root
        self._known_dirs: set[str] = set()

    @property
    def transport_encrypted(self) -> bool:
        return True

    def _path(self, *parts: str) -> str:
        return posixpath.join(self._root, *parts) if parts else self._root

    def _ensure_dir(self, path: str) -> None:
        absolute = path.startswith("/")
        current = "/" if absolute else ""
        for part in [segment for segment in path.split("/") if segment]:
            current = posixpath.join(current, part) if current else part
            if current in self._known_dirs:
                continue
            # lstat, never stat: a symlinked component could lead outside the
            # app-owned root, so only real directories are accepted.
            try:
                attributes = self._sftp.lstat(current)
            except OSError as exc:
                if not _is_missing(exc):
                    raise
                try:
                    self._sftp.mkdir(current)
                except OSError as mkdir_error:
                    # Lost a creation race, or a real error: re-check decides,
                    # and a still-missing directory surfaces the mkdir error.
                    try:
                        attributes = self._sftp.lstat(current)
                    except OSError:
                        raise mkdir_error from None
                else:
                    attributes = self._sftp.lstat(current)
            if not stat.S_ISDIR(attributes.st_mode or 0):
                raise ArchiveTransportError(f"SFTP archive path {current!r} is not a plain directory.")
            self._known_dirs.add(current)

    def put(self, local_path: Path, name: str) -> StoredObject:
        parts = validate_object_name(name)
        final = self._path(*parts)
        partial = final + PARTIAL_SUFFIX
        self._ensure_dir(posixpath.dirname(final))
        with _open_local_source(local_path) as (source, _size):
            try:
                with self._sftp.open(partial, "wb") as remote:
                    remote.set_pipelined(True)
                    size, sha = _copy_stream(source, remote.write)
                with self._sftp.open(partial, "rb") as readback:
                    readback.prefetch(size, max_concurrent_requests=SFTP_PREFETCH_MAX_REQUESTS)
                    back_size, back_sha = _hash_stream(readback)
                _check_readback("SFTP", size, sha, back_size, back_sha)
                try:
                    self._sftp.posix_rename(partial, final)
                except OSError:
                    # Server without the posix-rename extension: plain rename,
                    # which refuses to overwrite. An existing object is never
                    # removed to make room.
                    self._sftp.rename(partial, final)
            except BaseException:
                try:
                    self._sftp.remove(partial)
                except OSError:
                    logger.warning("SFTP archive could not remove an incomplete upload.")
                raise
        return StoredObject(name=name, size=size, sha256=sha, verified=True)

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = _validate_prefix(prefix)
        results: list[RemoteObject] = []
        self._walk("", results)
        return [item for item in results if item.name.startswith(prefix)]

    def _walk(self, relative: str, results: list[RemoteObject]) -> None:
        path = self._path(relative) if relative else self._root
        try:
            entries = self._sftp.listdir_attr(path)
        except OSError as exc:
            if not relative and _is_missing(exc):
                return
            raise
        for entry in sorted(entries, key=lambda item: item.filename):
            child = f"{relative}/{entry.filename}" if relative else entry.filename
            mode = entry.st_mode or 0
            if stat.S_ISDIR(mode):
                self._walk(child, results)
            elif stat.S_ISREG(mode) and _is_listable_name(child):
                modified = (
                    datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc) if entry.st_mtime is not None else None
                )
                results.append(RemoteObject(name=child, size=int(entry.st_size or 0), modified=modified))

    def delete(self, name: str) -> None:
        parts = validate_object_name(name)
        try:
            self._sftp.remove(self._path(*parts))
        except OSError as exc:
            if not _is_missing(exc):
                raise


@contextmanager
def _open_sftp(settings: ArchiveTargetSettings) -> Iterator[SftpTarget]:
    password = read_secret_file(settings.password_file, "SFTP password") if settings.password_file else None
    pkey = _load_private_key(settings)
    client = paramiko.SSHClient()
    try:
        _sftp_host_key_policy(client, settings)
        client.connect(
            hostname=settings.hostname,
            port=settings.effective_port or 22,
            username=settings.username,
            password=password,
            pkey=pkey,
            look_for_keys=False,
            allow_agent=False,
            timeout=settings.timeout_seconds,
            banner_timeout=settings.timeout_seconds,
            auth_timeout=settings.timeout_seconds,
        )
        sftp = client.open_sftp()
        try:
            root = "/".join(normalized_root_parts(settings.root))
            if settings.root.startswith("/"):
                root = "/" + root
            target = SftpTarget(sftp, root)
            target._ensure_dir(root)
            yield target
        finally:
            sftp.close()
    finally:
        client.close()


# --------------------------------------------------------------------------
# SMB (smbprotocol direct client, no mount)
# --------------------------------------------------------------------------


def _import_smbclient() -> Any:
    try:
        import smbclient
    except ImportError as exc:
        raise DependencyMissingError(
            "SMB archive support requires the smbprotocol package; install smbprotocol to use this target."
        ) from exc
    return smbclient


class SmbTarget(_TargetBase):
    provider = "smb"

    def __init__(self, client: Any, base: str, *, encrypted: bool) -> None:
        self._client = client
        self._base = base
        self._encrypted = encrypted

    @property
    def transport_encrypted(self) -> bool:
        return self._encrypted

    def _path(self, *parts: str) -> str:
        return "\\".join((self._base, *parts)) if parts else self._base

    def _ensure_dir(self, path: str) -> None:
        self._client.makedirs(path, exist_ok=True)
        if not stat.S_ISDIR(self._client.stat(path).st_mode):
            raise ArchiveTransportError("SMB archive path is not a directory.")

    def put(self, local_path: Path, name: str) -> StoredObject:
        parts = validate_object_name(name)
        final = self._path(*parts)
        partial = final + PARTIAL_SUFFIX
        self._ensure_dir(self._path(*parts[:-1]))
        with _open_local_source(local_path) as (source, _size):
            try:
                with self._client.open_file(partial, mode="wb") as remote:
                    size, sha = _copy_stream(source, remote.write)
                with self._client.open_file(partial, mode="rb") as readback:
                    back_size, back_sha = _hash_stream(readback)
                _check_readback("SMB", size, sha, back_size, back_sha)
                self._client.replace(partial, final)
            except BaseException:
                try:
                    self._client.remove(partial)
                except OSError:
                    logger.warning("SMB archive could not remove an incomplete upload.")
                raise
        return StoredObject(name=name, size=size, sha256=sha, verified=True)

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = _validate_prefix(prefix)
        results: list[RemoteObject] = []
        self._walk("", results)
        return [item for item in results if item.name.startswith(prefix)]

    def _walk(self, relative: str, results: list[RemoteObject]) -> None:
        path = self._path(*relative.split("/")) if relative else self._base
        try:
            entries = sorted(self._client.scandir(path), key=lambda item: item.name)
        except OSError as exc:
            if not relative and _is_missing(exc):
                return
            raise
        for entry in entries:
            child = f"{relative}/{entry.name}" if relative else entry.name
            if entry.is_dir():
                self._walk(child, results)
            elif entry.is_file() and _is_listable_name(child):
                info = entry.stat()
                results.append(
                    RemoteObject(
                        name=child,
                        size=int(info.st_size),
                        modified=datetime.fromtimestamp(info.st_mtime, tz=timezone.utc),
                    )
                )

    def delete(self, name: str) -> None:
        parts = validate_object_name(name)
        try:
            self._client.remove(self._path(*parts))
        except OSError as exc:
            if not _is_missing(exc):
                raise


@contextmanager
def _open_smb(settings: ArchiveTargetSettings) -> Iterator[SmbTarget]:
    client = _import_smbclient()
    password = read_secret_file(settings.password_file, "SMB password") if settings.password_file else None
    username = settings.username or None
    if username and settings.domain and "\\" not in username and "@" not in username:
        username = f"{settings.domain}\\{username}"
    port = settings.effective_port or 445
    client.register_session(
        settings.hostname,
        username=username,
        password=password,
        port=port,
        encrypt=True if settings.smb_encrypt else None,
        connection_timeout=settings.timeout_seconds,
    )
    try:
        base = "\\".join((f"\\\\{settings.hostname}\\{settings.share}", *normalized_root_parts(settings.root)))
        target = SmbTarget(client, base, encrypted=settings.smb_encrypt)
        target._ensure_dir(base)
        yield target
    finally:
        try:
            client.delete_session(settings.hostname, port=port)
        except Exception:  # best-effort disconnect; the session cache is per process
            logger.warning("SMB archive session could not be closed cleanly.")


# --------------------------------------------------------------------------
# NFS (mounted only for the duration of one block)
# --------------------------------------------------------------------------


def _run_mount_command(command: list[str], label: str) -> None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=MOUNT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArchiveTransportError(f"{label} timed out.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise ArchiveTransportError(f"{label} failed: {detail or 'exit code ' + str(result.returncode)}")


def _is_mounted(path: str) -> bool:
    return os.path.ismount(path)


def _nfs_mount_options(settings: ArchiveTargetSettings) -> str:
    options = [option.strip() for option in settings.mount_options.split(",") if option.strip()]
    if settings.port:
        options.append(f"port={settings.port}")
    return ",".join(options)


def _unmount_nfs(umount_bin: str, mount_dir: str) -> None:
    """Unmount, retrying, then lazily; raise NfsUnmountError if still mounted.

    Only ever calls ``os.rmdir`` on the mount point, and only after the mount
    is confirmed gone. ``rmdir`` cannot recurse, so a mount that is somehow
    still present (or a non-empty directory) is left untouched.
    """

    last_error: Exception | None = None
    for attempt in range(NFS_UMOUNT_ATTEMPTS):
        try:
            _run_mount_command([umount_bin, mount_dir], "unmount NFS archive")
        except ArchiveTransportError as exc:
            last_error = exc
        if not _is_mounted(mount_dir):
            break
        if attempt + 1 < NFS_UMOUNT_ATTEMPTS:
            time.sleep(NFS_UMOUNT_RETRY_DELAY_SECONDS)
    else:
        try:
            _run_mount_command([umount_bin, "-l", mount_dir], "lazy unmount NFS archive")
        except ArchiveTransportError as exc:
            last_error = exc
    if _is_mounted(mount_dir):
        raise NfsUnmountError(
            f"NFS archive is still mounted at {mount_dir}; the mount point was left in place. "
            f"Last error: {last_error}"
        )
    try:
        os.rmdir(mount_dir)
    except OSError as exc:
        logger.warning("NFS archive mount point %s was left in place: %s", mount_dir, exc.strerror)


@contextmanager
def _open_nfs(settings: ArchiveTargetSettings) -> Iterator[LocalDirectoryTarget]:
    mount_bin = shutil.which("mount")
    umount_bin = shutil.which("umount")
    if not mount_bin or not umount_bin:
        raise DependencyMissingError("NFS archive support requires mount and umount (nfs-common) in the container.")
    parent = settings.mount_parent or None
    mount_dir = tempfile.mkdtemp(prefix="archive-nfs-", dir=parent)
    command = [mount_bin, "-t", "nfs"]
    options = _nfs_mount_options(settings)
    if options:
        command.extend(["-o", options])
    command.extend([f"{settings.hostname}:{settings.export_path}", mount_dir])
    try:
        _run_mount_command(command, "mount NFS archive")
    except BaseException as mount_error:
        # Fail closed: never fall back to writing into the local directory.
        # A mount that timed out or failed may still have attached; unmount
        # it so the export is not left mounted after the job.
        if _is_mounted(mount_dir):
            try:
                _unmount_nfs(umount_bin, mount_dir)
            except NfsUnmountError as unmount_error:
                mount_error.add_note(str(unmount_error))
                logger.error("%s", unmount_error)
        else:
            try:
                os.rmdir(mount_dir)
            except OSError:
                pass
        raise
    if not _is_mounted(mount_dir):
        # mount reported success but nothing is mounted: refuse to write locally.
        try:
            os.rmdir(mount_dir)
        except OSError:
            pass
        raise ArchiveTransportError("NFS archive mount did not appear; refusing to write to a local directory.")
    body_error: BaseException | None = None
    try:
        root = Path(mount_dir).joinpath(*normalized_root_parts(settings.root))
        yield LocalDirectoryTarget(
            root,
            provider="nfs",
            confine_to=Path(mount_dir),
            encrypted=_nfs_encrypted(settings),
        )
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        try:
            _unmount_nfs(umount_bin, mount_dir)
        except NfsUnmountError as unmount_error:
            if body_error is None:
                raise
            body_error.add_note(str(unmount_error))
            logger.error("%s", unmount_error)


def _nfs_encrypted(settings: ArchiveTargetSettings) -> bool:
    return "sec=krb5p" in settings.mount_options.split(",")


# --------------------------------------------------------------------------
# S3 and S3-compatible
# --------------------------------------------------------------------------


def _import_boto3() -> tuple[Any, Any, Any]:
    try:
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config
    except ImportError as exc:
        raise DependencyMissingError(
            "S3 archive support requires the boto3 package; install boto3 to use this target."
        ) from exc
    return boto3, TransferConfig, Config


def _s3_expected_etag(md5_parts: list[bytes], whole_md5: str, *, multipart: bool) -> str:
    # boto3 switches to multipart at the threshold, so a file of exactly one
    # part still gets the "<md5-of-md5s>-1" form.
    if not multipart:
        return whole_md5
    combined = hashlib.md5(b"".join(md5_parts), usedforsecurity=False).hexdigest()
    return f"{combined}-{len(md5_parts)}"


class S3Target(_TargetBase):
    provider = "s3"

    def __init__(self, client: Any, bucket: str, prefix: str, *, transfer_config: Any, encrypted: bool) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix
        self._transfer_config = transfer_config
        self._encrypted = encrypted

    @property
    def transport_encrypted(self) -> bool:
        return self._encrypted

    def _key(self, name: str) -> str:
        return f"{self._prefix}/{name}"

    def put(self, local_path: Path, name: str) -> StoredObject:
        validate_object_name(name)
        key = self._key(name)
        with _open_local_source(local_path) as (source, _size):
            # First pass: SHA-256 (stored as metadata) and the MD5s S3 will
            # report as the ETag for our fixed part size.
            sha = hashlib.sha256()
            whole_md5 = hashlib.md5(usedforsecurity=False)
            part_md5s: list[bytes] = []
            size = 0
            multipart = os.fstat(source.fileno()).st_size >= S3_MULTIPART_THRESHOLD
            part = hashlib.md5(usedforsecurity=False)
            part_filled = 0
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha.update(chunk)
                whole_md5.update(chunk)
                size += len(chunk)
                if multipart:
                    offset = 0
                    while offset < len(chunk):
                        take = min(S3_MULTIPART_CHUNKSIZE - part_filled, len(chunk) - offset)
                        part.update(chunk[offset : offset + take])
                        part_filled += take
                        offset += take
                        if part_filled == S3_MULTIPART_CHUNKSIZE:
                            part_md5s.append(part.digest())
                            part = hashlib.md5(usedforsecurity=False)
                            part_filled = 0
            if multipart and part_filled:
                part_md5s.append(part.digest())
            sha_hex = sha.hexdigest()
            source.seek(0)
            # boto3 streams a seekable file part by part (multipart above the
            # threshold); it never loads the whole file.
            self._client.upload_fileobj(
                source,
                self._bucket,
                key,
                ExtraArgs={"Metadata": {"sha256": sha_hex}},
                Config=self._transfer_config,
            )
        try:
            verified = self._verify(key, size, sha_hex, _s3_expected_etag(part_md5s, whole_md5.hexdigest(), multipart=multipart))
        except ArchiveVerificationError:
            self._delete_quietly(key)
            raise
        return StoredObject(name=name, size=size, sha256=sha_hex, verified=verified)

    def _verify(self, key: str, size: int, sha_hex: str, expected_etag: str) -> bool:
        head = self._client.head_object(Bucket=self._bucket, Key=key)
        remote_size = int(head.get("ContentLength", -1))
        _check_readback("S3", size, sha_hex, remote_size, None)
        stored_sha = (head.get("Metadata") or {}).get("sha256")
        if stored_sha is not None and stored_sha != sha_hex:
            raise ArchiveVerificationError("S3 archive stored SHA-256 metadata does not match the bytes sent.")
        etag = str(head.get("ETag") or "").strip('"')
        if etag and etag == expected_etag:
            return True
        if size <= S3_REGET_LIMIT:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"]
            try:
                back_size, back_sha = _hash_stream(body)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            _check_readback("S3", size, sha_hex, back_size, back_sha)
            return True
        # Large object whose ETag is not a plain MD5 (for example SSE-KMS):
        # size matched, content not independently confirmed.
        return False

    def _delete_quietly(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception:  # cleanup after a failed verify must not mask it
            logger.warning("S3 archive could not remove an unverified object.")

    def list(self, prefix: str = "") -> list[RemoteObject]:
        prefix = _validate_prefix(prefix)
        paginator = self._client.get_paginator("list_objects_v2")
        results: list[RemoteObject] = []
        root = self._prefix + "/"
        for page in paginator.paginate(Bucket=self._bucket, Prefix=root + prefix):
            for item in page.get("Contents", []) or []:
                key = str(item.get("Key") or "")
                if not key.startswith(root):
                    continue
                name = key[len(root) :]
                if not _is_listable_name(name):
                    continue
                modified = item.get("LastModified")
                if isinstance(modified, datetime) and modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                results.append(
                    RemoteObject(
                        name=name,
                        size=int(item.get("Size") or 0),
                        modified=modified if isinstance(modified, datetime) else None,
                    )
                )
        return sorted(results, key=lambda item: item.name)

    def delete(self, name: str) -> None:
        validate_object_name(name)
        self._client.delete_object(Bucket=self._bucket, Key=self._key(name))


@contextmanager
def _open_s3(settings: ArchiveTargetSettings) -> Iterator[S3Target]:
    boto3, transfer_config_class, botocore_config_class = _import_boto3()
    client_kwargs: dict[str, Any] = {
        "aws_access_key_id": read_secret_file(settings.access_key_id_file, "S3 access key id"),
        "aws_secret_access_key": read_secret_file(settings.secret_access_key_file, "S3 secret access key"),
        "config": botocore_config_class(
            connect_timeout=settings.timeout_seconds,
            read_timeout=settings.timeout_seconds,
            retries={"max_attempts": 3},
        ),
    }
    if settings.endpoint_url:
        client_kwargs["endpoint_url"] = settings.endpoint_url
    if settings.region:
        client_kwargs["region_name"] = settings.region
    client = boto3.client("s3", **client_kwargs)
    transfer_config = transfer_config_class(
        multipart_threshold=S3_MULTIPART_THRESHOLD,
        multipart_chunksize=S3_MULTIPART_CHUNKSIZE,
        use_threads=False,
    )
    try:
        yield S3Target(
            client,
            settings.bucket,
            "/".join(normalized_root_parts(settings.root)),
            transfer_config=transfer_config,
            encrypted=not settings.endpoint_url.startswith("http://"),
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


@contextmanager
def open_target(settings: ArchiveTargetSettings) -> Iterator[ArchiveTarget]:
    """Open one connection (or NFS mount) for the whole block and close it on exit."""

    provider = settings.provider
    if provider == "filesystem":
        yield LocalDirectoryTarget(Path("/").joinpath(*normalized_root_parts(settings.root)))
    elif provider == "ftp":
        with _open_ftp(settings) as target:
            yield target
    elif provider == "sftp":
        with _open_sftp(settings) as target:
            yield target
    elif provider == "smb":
        with _open_smb(settings) as target:
            yield target
    elif provider == "nfs":
        with _open_nfs(settings) as target:
            yield target
    elif provider == "s3":
        with _open_s3(settings) as target:
            yield target
    else:  # validated in settings; kept as a guard
        raise ArchiveConfigError(f"Unsupported archive provider: {provider!r}")


def transport_encrypted(settings: ArchiveTargetSettings) -> bool:
    """Whether a target's transport is encrypted, without connecting (for UI labels)."""

    provider = settings.provider
    if provider in {"filesystem", "sftp"}:
        return True
    if provider == "ftp":
        return settings.use_tls
    if provider == "smb":
        return settings.smb_encrypt
    if provider == "nfs":
        return _nfs_encrypted(settings)
    if provider == "s3":
        return not settings.endpoint_url.startswith("http://")
    return False
