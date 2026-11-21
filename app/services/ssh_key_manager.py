from __future__ import annotations

import base64
import errno
import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from app.config import Settings
from app.services.atomic_write import write_atomically


def configured_key_paths(settings: Settings) -> tuple[str, ...]:
    paths = (settings.ssh.key_path, *(system.ssh.key_path for system in settings.systems))
    return tuple(dict.fromkeys(path for path in paths if path))


def private_key_warnings(paths: tuple[str, ...]) -> list[str]:
    """Inspect only existing configured entries. Never open a key or follow its final symlink."""
    warnings = []
    for path in paths:
        label = f"SSH private key {path!r}"
        try:
            info = Path(path).lstat()
        except FileNotFoundError:
            # The default key path can be unused (SSH off or password-only).
            continue
        except (OSError, ValueError):
            warnings.append(f"{label}: cannot check permissions. Check the path and directory access.")
            continue
        remedy = "Use chmod 600 for owner-only access or chmod 640 with the application group (APP_GID)."
        if not stat.S_ISREG(info.st_mode):
            reason = "is a symlink" if stat.S_ISLNK(info.st_mode) else "is not a regular file"
            warnings.append(f"{label} {reason}. Configure a regular private key file instead. {remedy}")
            continue
        try:
            # Both services use the UI identity, even when admin runs as root.
            uid = int(os.getenv("APP_UID") or "10001")
            gid = int(os.getenv("APP_GID") or "10001")
        except ValueError:
            warnings.append(f"{label}: cannot check ownership. Set APP_UID and APP_GID to numeric IDs.")
            continue
        if (info.st_uid not in (0, uid) or info.st_mode & (stat.S_IRWXO | stat.S_IWGRP)
                or (info.st_mode & stat.S_IRGRP and info.st_gid != gid)):
            warnings.append(f"{label} has unsafe ownership or permissions. Set the owner to root or APP_UID. {remedy}")
    return warnings


def list_keys_with_warnings(manager: SSHKeyManager, settings: Settings) -> tuple[list[dict[str, Any]], list[str]]:
    # A configured key need not be in the picker directory or parseable.
    return manager.list_keys(), private_key_warnings(configured_key_paths(settings))


class SSHKeyManager:
    def __init__(self, config_path: str, runtime_dir: str = "/run/ssh") -> None:
        self.config_path = Path(config_path)
        self.key_dir = self.config_path.parent / "ssh"
        self.runtime_dir = PurePosixPath(runtime_dir)

    def list_keys(self) -> list[dict[str, Any]]:
        self.key_dir.mkdir(parents=True, exist_ok=True)
        keys: list[dict[str, Any]] = []
        for path in sorted(self.key_dir.iterdir(), key=lambda item: item.name.lower()):
            if not self._is_private_key_candidate(path):
                continue
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    keys.append(self._describe_private_key(path))
            except (ValueError, FileNotFoundError, PermissionError):
                continue
            except OSError as exc:
                if exc.errno != errno.ELOOP:  # Replaced by a symlink during listing.
                    raise
        return keys

    def generate_keypair(self, name: str) -> dict[str, Any]:
        normalized_name = self._normalize_key_name(name)
        if not normalized_name:
            raise ValueError("A key name is required.")

        self.key_dir.mkdir(parents=True, exist_ok=True)
        private_path = self.key_dir / normalized_name
        public_path = Path(f"{private_path}.pub")
        if private_path.exists() or public_path.exists():
            raise ValueError(f"An SSH key named '{normalized_name}' already exists.")

        private_key = ed25519.Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_text = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode("ascii")

        # The private key is created group-readable for the app group and never
        # broader: it does not pass through the umask default on the way in.
        self._write_bytes_atomic(private_path, private_bytes, mode=0o640)
        self._write_bytes_atomic(
            public_path,
            f"{public_text} truenas-jbod-ui:{normalized_name}\n".encode("utf-8"),
            mode=0o644,
        )
        return self._describe_private_key(private_path)

    @staticmethod
    def _normalize_key_name(name: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(name or "").strip()).strip(".-_")
        return normalized[:128].lower()

    @staticmethod
    def _is_private_key_candidate(path: Path) -> bool:
        name = path.name
        if name.startswith("."):
            return False
        if name.endswith((".pub", ".tmp", ".bak", ".old")):
            return False
        if name in {"authorized_keys", "known_hosts"}:
            return False
        return True

    def _describe_private_key(self, path: Path) -> dict[str, Any]:
        # Verify the opened file too: a directory entry may change during listing.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("File is not a regular private key.")
            private_key = self._load_private_key(handle.read())
        public_text = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode("ascii")
        public_path = Path(f"{path}.pub")
        runtime_public_path = self.runtime_dir / public_path.name
        return {
            "name": path.name,
            "algorithm": public_text.split(" ", 1)[0],
            "private_path": str(path),
            "public_path": str(public_path) if public_path.exists() else None,
            "runtime_private_path": str(self.runtime_dir / path.name),
            "runtime_public_path": str(runtime_public_path) if public_path.exists() else None,
            "public_key": public_text,
            "fingerprint": self._build_fingerprint(public_text),
        }

    @staticmethod
    def _load_private_key(payload: bytes):
        for loader in (
            serialization.load_ssh_private_key,
            serialization.load_pem_private_key,
        ):
            try:
                return loader(payload, password=None)
            except (TypeError, ValueError):
                continue
        raise ValueError("File is not a supported private key.")

    @staticmethod
    def _build_fingerprint(public_text: str) -> str:
        parts = public_text.strip().split()
        if len(parts) < 2:
            raise ValueError("SSH public key payload is malformed.")
        key_blob = base64.b64decode(parts[1].encode("ascii"))
        digest = hashlib.sha256(key_blob).digest()
        return f"SHA256:{base64.b64encode(digest).decode('ascii').rstrip('=')}"

    @staticmethod
    def _write_bytes_atomic(path: Path, content: bytes, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(path, lambda handle: handle.write(content), binary=True, mode=mode)
