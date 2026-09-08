from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable


RECEIPT_VERSION = 1
DEFAULT_RECEIPT_TTL_SECONDS = 300
DEFAULT_MAX_ACTIVE_ADMISSIONS = 32
HASH_CHUNK_BYTES = 1024 * 1024
_VALID_MODES = frozenset({"encrypted", "plaintext"})


def _b64url_encode(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _b64url_decode(content: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", content):
        raise ValueError("Backup inspection receipt is invalid.")
    try:
        return base64.urlsafe_b64decode(content + "=" * (-len(content) % 4))
    except ValueError as exc:
        raise ValueError("Backup inspection receipt is invalid.") from exc


def _archive_sha256(archive_path: Path) -> str:
    digest = hashlib.sha256()
    with archive_path.open("rb", buffering=0) as source:
        while chunk := source.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


class BackupInspectionReceiptStore:
    """Process-local, single-use inspection receipts for import admission."""

    def __init__(
        self,
        *,
        signing_key: bytes | None = None,
        ttl_seconds: int = DEFAULT_RECEIPT_TTL_SECONDS,
        nonce_factory: Callable[[], bytes] | None = None,
        max_active_admissions: int = DEFAULT_MAX_ACTIVE_ADMISSIONS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Backup inspection receipt TTL must be positive.")
        if max_active_admissions <= 0:
            raise ValueError("Backup inspection admission limit must be positive.")
        self._signing_key = signing_key or secrets.token_bytes(32)
        if len(self._signing_key) < 32:
            raise ValueError("Backup inspection receipt signing key is too short.")
        self._ttl_seconds = ttl_seconds
        self._max_active_admissions = max_active_admissions
        self._nonce_factory = nonce_factory or (lambda: secrets.token_bytes(16))
        self._lock = threading.Lock()
        self._issued: dict[str, tuple[str, str, int, int, bool]] = {}
        self._admissions: dict[str, str] = {}
        self._admitted_nonces: set[str] = set()

    @staticmethod
    def _validate_mode(mode: str) -> str:
        if mode not in _VALID_MODES:
            raise ValueError("Backup encryption mode must be encrypted or plaintext.")
        return mode

    def issue(
        self,
        archive_path: Path,
        *,
        observed_encryption_mode: str,
        now: int | None = None,
    ) -> dict[str, int | str]:
        return self.issue_digest(
            _archive_sha256(archive_path),
            observed_encryption_mode=observed_encryption_mode,
            now=now,
        )

    def issue_digest(
        self,
        archive_digest: str,
        *,
        observed_encryption_mode: str,
        now: int | None = None,
    ) -> dict[str, int | str]:
        mode = self._validate_mode(observed_encryption_mode)
        if not re.fullmatch(r"[0-9a-f]{64}", archive_digest):
            raise ValueError("Backup archive identity is invalid.")
        issued_at = int(time.time() if now is None else now)
        expires_at = issued_at + self._ttl_seconds
        with self._lock:
            self._prune_expired_locked(issued_at)
            for _ in range(8):
                nonce = self._nonce_factory().hex()
                if re.fullmatch(r"[0-9a-f]{32}", nonce) and nonce not in self._issued:
                    break
            else:
                raise RuntimeError("Unable to allocate a backup inspection receipt nonce.")
            self._issued[nonce] = (
                archive_digest,
                mode,
                issued_at,
                expires_at,
                False,
            )
        payload = {
            "version": RECEIPT_VERSION,
            "archive_sha256": archive_digest,
            "encryption_mode": mode,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "nonce": nonce,
        }
        encoded_payload = _b64url_encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(
            self._signing_key,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return {
            "receipt": f"{encoded_payload}.{_b64url_encode(signature)}",
            "expires_at": expires_at,
        }

    def consume(
        self,
        receipt: str,
        archive_path: Path,
        *,
        expected_encryption_mode: str,
        admission: str | None = None,
        now: int | None = None,
    ) -> None:
        self.consume_digest(
            receipt,
            _archive_sha256(archive_path),
            expected_encryption_mode=expected_encryption_mode,
            admission=admission,
            now=now,
        )

    def begin_admission(
        self,
        receipt: str,
        *,
        expected_encryption_mode: str,
        now: int | None = None,
    ) -> str:
        expected_mode = self._validate_mode(expected_encryption_mode)
        current_time = int(time.time() if now is None else now)
        receipt_digest, mode, issued_at, expires_at, nonce = self._decode_receipt(receipt)
        if current_time > expires_at:
            raise ValueError("Backup inspection receipt has expired.")
        if mode != expected_mode:
            raise ValueError(
                "Backup inspection receipt encryption mode does not match the import mode."
            )

        expected_record = (receipt_digest, mode, issued_at, expires_at)
        with self._lock:
            record = self._issued.get(nonce)
            if record is None or record[:4] != expected_record:
                raise ValueError("Backup inspection receipt was not issued by this server.")
            if record[4]:
                raise ValueError("Backup inspection receipt was already used.")
            if nonce in self._admitted_nonces:
                raise ValueError("Backup inspection receipt is already being imported.")
            if len(self._admissions) >= self._max_active_admissions:
                raise ValueError("Too many backup imports are already in progress.")
            for _ in range(8):
                admission = secrets.token_hex(16)
                if admission not in self._admissions:
                    break
            else:
                raise RuntimeError("Unable to allocate a backup inspection admission.")
            self._admissions[admission] = nonce
            self._admitted_nonces.add(nonce)
        return admission

    def release_admission(self, admission: str, *, now: int | None = None) -> None:
        current_time = int(time.time() if now is None else now)
        with self._lock:
            nonce = self._admissions.pop(admission, None)
            if nonce is not None:
                self._admitted_nonces.discard(nonce)
            self._prune_expired_locked(current_time)

    def consume_digest(
        self,
        receipt: str,
        archive_digest: str,
        *,
        expected_encryption_mode: str,
        admission: str | None = None,
        now: int | None = None,
    ) -> None:
        expected_mode = self._validate_mode(expected_encryption_mode)
        if not re.fullmatch(r"[0-9a-f]{64}", archive_digest):
            raise ValueError("Backup archive identity is invalid.")
        current_time = int(time.time() if now is None else now)
        receipt_digest, mode, issued_at, expires_at, nonce = self._decode_receipt(receipt)
        if current_time > expires_at:
            raise ValueError("Backup inspection receipt has expired.")
        if mode != expected_mode:
            raise ValueError("Backup inspection receipt encryption mode does not match the import mode.")
        if archive_digest != receipt_digest:
            raise ValueError("Backup archive does not match its inspection receipt.")

        expected_record = (receipt_digest, mode, issued_at, expires_at)
        with self._lock:
            record = self._issued.get(nonce)
            if record is None or record[:4] != expected_record:
                raise ValueError("Backup inspection receipt was not issued by this server.")
            if record[4]:
                raise ValueError("Backup inspection receipt was already used.")
            if admission is None:
                if nonce in self._admitted_nonces:
                    raise ValueError("Backup inspection receipt is already being imported.")
            elif self._admissions.get(admission) != nonce:
                raise ValueError("Backup inspection receipt admission is invalid.")
            self._issued[nonce] = (*record[:4], True)
            if admission is not None:
                self._admissions.pop(admission, None)
                self._admitted_nonces.discard(nonce)
            self._prune_expired_locked(current_time)

    def _decode_receipt(self, receipt: str) -> tuple[str, str, int, int, str]:
        try:
            encoded_payload, encoded_signature = receipt.split(".", 1)
        except ValueError as exc:
            raise ValueError("Backup inspection receipt is invalid.") from exc
        expected_signature = hmac.new(
            self._signing_key,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url_decode(encoded_signature), expected_signature):
            raise ValueError("Backup inspection receipt is invalid.")
        try:
            payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Backup inspection receipt is invalid.") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "archive_sha256",
            "encryption_mode",
            "issued_at",
            "expires_at",
            "nonce",
        }:
            raise ValueError("Backup inspection receipt is invalid.")
        version = payload["version"]
        receipt_digest = payload["archive_sha256"]
        mode = payload["encryption_mode"]
        issued_at = payload["issued_at"]
        expires_at = payload["expires_at"]
        nonce = payload["nonce"]
        if (
            type(version) is not int
            or version != RECEIPT_VERSION
            or not isinstance(receipt_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt_digest)
            or mode not in _VALID_MODES
            or type(issued_at) is not int
            or type(expires_at) is not int
            or expires_at != issued_at + self._ttl_seconds
            or not isinstance(nonce, str)
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
        ):
            raise ValueError("Backup inspection receipt is invalid.")
        return receipt_digest, mode, issued_at, expires_at, nonce

    def _prune_expired_locked(self, now: int) -> None:
        self._issued = {
            nonce: record
            for nonce, record in self._issued.items()
            if record[3] >= now or nonce in self._admitted_nonces
        }
