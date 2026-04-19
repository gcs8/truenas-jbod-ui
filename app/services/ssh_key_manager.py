from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


class SSHKeyManager:
    def __init__(self, config_path: str, runtime_dir: str = "/run/ssh") -> None:
        self.config_path = Path(config_path)
        self.key_dir = self.config_path.parent / "ssh"
        self.runtime_dir = PurePosixPath(runtime_dir)

    def list_keys(self) -> list[dict[str, Any]]:
        self.key_dir.mkdir(parents=True, exist_ok=True)
        keys: list[dict[str, Any]] = []
        for path in sorted(self.key_dir.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file():
                continue
            if not self._is_private_key_candidate(path):
                continue
            try:
                keys.append(self._describe_private_key(path))
            except ValueError:
                continue
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

        self._write_bytes_atomic(private_path, private_bytes)
        self._write_bytes_atomic(public_path, f"{public_text} truenas-jbod-ui:{normalized_name}\n".encode("utf-8"))
        self._set_permissions(private_path, 0o600)
        self._set_permissions(public_path, 0o644)
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
        private_key = self._load_private_key(path.read_bytes())
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
    def _write_bytes_atomic(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_bytes(content)
        temp_path.replace(path)

    @staticmethod
    def _set_permissions(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass
