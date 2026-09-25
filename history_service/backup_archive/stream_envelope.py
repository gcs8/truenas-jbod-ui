"""Chunked, authenticated encryption for large backup archives (#397).

``TJBENC01`` (the scheduled config-backup envelope) is one AES-256-GCM message
over the whole archive. Its tag sits at the end, so a reader only learns that
the data is genuine after decrypting every byte. That is acceptable for small
config archives but not for a multi-GiB FULL backup.

``TJBENC02`` uses the STREAM construction (Hoang, Reyhanitabar, Rogaway and
Vizar, 2015) over AES-256-GCM:

* header (48 bytes): ``TJBENC02`` magic, a format version byte, the scrypt
  parameters (log2 N, r, p), log2 of the chunk size, 3 zero bytes, a 16-byte
  salt, a 7-byte nonce prefix, and 9 zero bytes of padding;
* then one record per plaintext chunk: ciphertext followed by its 16-byte tag.
  Every record except the last holds exactly ``chunk_size`` plaintext bytes.
  The last one holds 0 to ``chunk_size`` bytes;
* the nonce of record *i* is ``prefix(7) || i (4, big endian) || last flag(1)``,
  and every record authenticates the whole header as associated data.

Every record is checked before its plaintext is written, so memory stays at
one chunk. Truncation, reordering, dropping the final record, trailing data
and header edits all fail authentication. The key is derived with scrypt from
the passphrase. The passphrase never leaves this process.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"TJBENC02"
FORMAT_VERSION = 1
HEADER_BYTES = 48
TAG_BYTES = 16
SALT_BYTES = 16
NONCE_PREFIX_BYTES = 7
DEFAULT_CHUNK_LOG2 = 20  # 1 MiB plaintext per record
DEFAULT_SCRYPT = (15, 8, 1)  # log2 N, r, p, the same cost as TJBENC01
# Readers refuse anything outside these bounds, so a crafted header cannot ask
# for a huge key-derivation cost or chunk buffer.
_CHUNK_LOG2_RANGE = range(16, 25)  # 64 KiB .. 16 MiB
_SCRYPT_LOG2_N_RANGE = range(14, 21)
_SCRYPT_R_RANGE = range(1, 17)
_SCRYPT_P_RANGE = range(1, 5)
_SCRYPT_MAX_MEMORY = 512 * 1024 * 1024
_MAX_RECORDS = 2**32 - 1
_HEADER = struct.Struct(">8sBBBBB3s16s7s9s")
assert _HEADER.size == HEADER_BYTES

DECRYPT_FAILED = (
    "The backup could not be decrypted. Check the passphrase; if it is right, the file is damaged or incomplete."
)
CORRUPT = "The encrypted backup file is damaged or incomplete."


class StreamEnvelopeError(ValueError):
    """Plain, secret-free reason an envelope could not be read or written."""


def is_stream_envelope(prefix: bytes) -> bool:
    return prefix.startswith(MAGIC)


def _derive_key(passphrase: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**log2_n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    if index > _MAX_RECORDS:
        raise StreamEnvelopeError("The backup is too large for one encrypted file.")
    return prefix + index.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


class StreamSealer:
    """File-like writer: ``write()`` plaintext and it writes records to ``output``.

    Call :meth:`close` exactly once to seal the final record. Without it the
    output is incomplete and a reader refuses it.
    """

    def __init__(
        self,
        output: BinaryIO,
        passphrase: str,
        *,
        chunk_log2: int = DEFAULT_CHUNK_LOG2,
        scrypt: tuple[int, int, int] = DEFAULT_SCRYPT,
    ) -> None:
        if not passphrase:
            raise StreamEnvelopeError("A passphrase is required when encryption is enabled.")
        if chunk_log2 not in _CHUNK_LOG2_RANGE:
            raise StreamEnvelopeError("Unsupported encryption chunk size.")
        log2_n, r, p = scrypt
        salt = os.urandom(SALT_BYTES)
        self._prefix = os.urandom(NONCE_PREFIX_BYTES)
        self._header = _HEADER.pack(
            MAGIC, FORMAT_VERSION, log2_n, r, p, chunk_log2, b"\0" * 3, salt, self._prefix, b"\0" * 9
        )
        self._aead = AESGCM(_derive_key(passphrase, salt, log2_n, r, p))
        self._chunk = 1 << chunk_log2
        self._buffer = bytearray()
        self._index = 0
        self._output = output
        self._closed = False
        self.bytes_in = 0
        self.bytes_out = HEADER_BYTES
        output.write(self._header)

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        if self._closed:
            raise StreamEnvelopeError("The encrypted backup was already sealed.")
        view = memoryview(data)
        self.bytes_in += len(view)
        self._buffer += view
        # Keep at least one byte back so the final record is never an unmarked full chunk.
        while len(self._buffer) > self._chunk:
            self._seal(bytes(self._buffer[: self._chunk]), last=False)
            del self._buffer[: self._chunk]
        return len(view)

    def flush(self) -> None:
        self._output.flush()

    def _seal(self, chunk: bytes, *, last: bool) -> None:
        record = self._aead.encrypt(_nonce(self._prefix, self._index, last), chunk, self._header)
        self._output.write(record)
        self.bytes_out += len(record)
        self._index += 1

    def close(self) -> None:
        if self._closed:
            return
        self._seal(bytes(self._buffer), last=True)
        self._buffer.clear()
        self._closed = True
        self._output.flush()


def encrypt_file(source_path: Path, output_path: Path, passphrase: str, *, read_bytes: int = 1 << 20) -> int:
    """Encrypt ``source_path`` into a new private ``output_path``; return its size."""

    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output, source_path.open("rb") as source:
            sealer = StreamSealer(output, passphrase)
            while chunk := source.read(read_bytes):
                sealer.write(chunk)
            sealer.close()
            output.flush()
            os.fsync(output.fileno())
            return sealer.bytes_out
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


def read_header(source: BinaryIO) -> tuple[bytes, int, tuple[int, int, int], bytes, bytes]:
    header = source.read(HEADER_BYTES)
    if len(header) != HEADER_BYTES:
        raise StreamEnvelopeError(CORRUPT)
    magic, version, log2_n, r, p, chunk_log2, reserved, salt, prefix, padding = _HEADER.unpack(header)
    if magic != MAGIC:
        raise StreamEnvelopeError(CORRUPT)
    if version != FORMAT_VERSION:
        raise StreamEnvelopeError(
            "This encrypted backup was made by a newer version of the app. Update the app, then restore it."
        )
    if (
        reserved != b"\0" * 3
        or padding != b"\0" * 9
        or chunk_log2 not in _CHUNK_LOG2_RANGE
        or log2_n not in _SCRYPT_LOG2_N_RANGE
        or r not in _SCRYPT_R_RANGE
        or p not in _SCRYPT_P_RANGE
        or 128 * r * (2**log2_n) * p > _SCRYPT_MAX_MEMORY
    ):
        raise StreamEnvelopeError(CORRUPT)
    return header, 1 << chunk_log2, (log2_n, r, p), salt, prefix


def decrypt_file(
    source_path: Path,
    output_path: Path,
    passphrase: str | None,
    *,
    max_output_bytes: int,
) -> int:
    """Decrypt into a new private ``output_path``; return the plaintext size.

    Nothing unauthenticated is ever written. On any failure the partial output
    is removed and :class:`StreamEnvelopeError` explains the failure without
    revealing any secret.
    """

    if not passphrase:
        raise StreamEnvelopeError("A passphrase is required for this encrypted backup.")
    total_size = source_path.stat(follow_symlinks=False).st_size
    with source_path.open("rb", buffering=0) as source:
        header, chunk_size, (log2_n, r, p), salt, prefix = read_header(source)
        body = total_size - HEADER_BYTES
        record_size = chunk_size + TAG_BYTES
        if body < TAG_BYTES:
            raise StreamEnvelopeError(CORRUPT)
        full_records, remainder = divmod(body, record_size)
        if remainder == 0:
            records = full_records  # the last record is a full chunk
        elif remainder < TAG_BYTES:
            raise StreamEnvelopeError(CORRUPT)
        else:
            records = full_records + 1
        plaintext_size = body - records * TAG_BYTES
        if plaintext_size > max_output_bytes:
            raise StreamEnvelopeError("The encrypted backup is larger than this app accepts.")
        aead = AESGCM(_derive_key(passphrase, salt, log2_n, r, p))
        descriptor = os.open(
            output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                for index in range(records):
                    last = index == records - 1
                    wanted = record_size if not last or remainder == 0 else remainder
                    record = source.read(wanted)
                    if len(record) != wanted:
                        raise StreamEnvelopeError(CORRUPT)
                    try:
                        plaintext = aead.decrypt(_nonce(prefix, index, last), record, header)
                    except InvalidTag as exc:
                        raise StreamEnvelopeError(DECRYPT_FAILED if index == 0 else CORRUPT) from exc
                    output.write(plaintext)
                    written += len(plaintext)
                if source.read(1):
                    raise StreamEnvelopeError(CORRUPT)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            output_path.unlink(missing_ok=True)
            raise
    if written != plaintext_size:
        output_path.unlink(missing_ok=True)
        raise StreamEnvelopeError(CORRUPT)
    return written


__all__ = [
    "MAGIC",
    "StreamEnvelopeError",
    "StreamSealer",
    "decrypt_file",
    "encrypt_file",
    "is_stream_envelope",
    "read_header",
]
