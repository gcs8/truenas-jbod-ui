"""Durable catalog of every backup artifact the app wrote.

The catalog is the only source grooming acts on: an object a backup location
holds is eligible for deletion only when it has a catalog record here, never
because its name looks like a backup. Each record names one copy of one backup
(``backup_class``) at one ``location`` (``"local"`` or a remote target id).

Storage is a small SQLite database in a private directory (WAL journal, a
``schema_version`` table). Deleting a copy moves its record into
``tombstones`` with the time, reason and actor, and pin/unpin actions are kept
in ``preserve_events``, so the history of what was removed and why stays
auditable after the artifact itself is gone.

Deleting a stored object is a three-step protocol so a pin can never race a
deletion: ``claim_deletion`` atomically re-checks the record and marks it as
being deleted (``set_preserve`` refuses while a claim is held), the caller
removes the object, then ``record_deletion`` tombstones it or
``release_deletion`` drops the claim. A claim left by a crash stays visible in
``claimed_deletions()`` and keeps the artifact out of later plans.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

CATALOG_SCHEMA_VERSION = 1
BACKUP_CLASSES: tuple[str, ...] = ("config", "full")
LOCAL_LOCATION = "local"

BackupClass = Literal["config", "full"]

_ARTIFACT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LOCATION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TEXT = 512
_MAX_CHANGE_IDS = 10_000

_SCHEMA = (
    """
    CREATE TABLE schema_version (
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY,
        backup_class TEXT NOT NULL CHECK (backup_class IN ('config', 'full')),
        location TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        sha256 TEXT NOT NULL,
        verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
        verified_at TEXT,
        change_ids TEXT NOT NULL DEFAULT '[]',
        preserved INTEGER NOT NULL DEFAULT 0 CHECK (preserved IN (0, 1)),
        preserve_reason TEXT NOT NULL DEFAULT '',
        preserved_by TEXT NOT NULL DEFAULT '',
        preserved_at TEXT,
        deletion_claimed_at TEXT,
        deletion_claimed_by TEXT NOT NULL DEFAULT '',
        cataloged_at TEXT NOT NULL,
        UNIQUE (location, name)
    )
    """,
    """
    CREATE INDEX artifacts_by_group
        ON artifacts (backup_class, location, created_at, artifact_id)
    """,
    """
    CREATE TABLE tombstones (
        artifact_id TEXT PRIMARY KEY,
        backup_class TEXT NOT NULL,
        location TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        verified INTEGER NOT NULL,
        change_ids TEXT NOT NULL,
        deleted_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        actor TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE preserve_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        artifact_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('preserve', 'unpreserve')),
        reason TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
)


class CatalogError(ValueError):
    """The catalog refused an operation (bad input, unknown or duplicate id)."""


def _newest_verified_id(connection: sqlite3.Connection, backup_class: str, location: str) -> str | None:
    row = connection.execute(
        "SELECT artifact_id FROM artifacts WHERE backup_class = ? AND location = ? AND verified = 1 "
        "ORDER BY created_at DESC, artifact_id DESC LIMIT 1",
        (backup_class, location),
    ).fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    backup_class: BackupClass
    location: str
    name: str
    created_at: datetime
    size: int
    sha256: str
    verified: bool
    change_ids: tuple[str, ...] = ()
    preserved: bool = False
    preserve_reason: str = ""
    preserved_by: str = ""


@dataclass(frozen=True, slots=True)
class Tombstone:
    """What was deleted, when, why and by whom (the record at deletion time)."""

    record: ArtifactRecord
    deleted_at: datetime
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class PreserveEvent:
    artifact_id: str
    action: Literal["preserve", "unpreserve"]
    reason: str
    actor: str
    at: datetime


def new_artifact_id() -> str:
    """An opaque, server-issued artifact id."""

    return secrets.token_hex(16)


def utc(value: datetime) -> datetime:
    """``value`` in UTC; naive datetimes are refused rather than guessed."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CatalogError("Timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    # One fixed-width format so the text sorts in time order inside SQLite.
    return utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _timestamp_value(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def validate_object_name(name: str) -> str:
    """A relative POSIX object name: no absolute path, ``..``, backslash or empty part."""

    if not isinstance(name, str) or not name or len(name) > 1024:
        raise CatalogError("Artifact name is invalid.")
    if "\\" in name or "\x00" in name or name.startswith("/"):
        raise CatalogError("Artifact name is invalid.")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CatalogError("Artifact name is invalid.")
    if any(ord(char) < 32 for char in name):
        raise CatalogError("Artifact name is invalid.")
    return name


def validate_location(location: str) -> str:
    if not isinstance(location, str) or not _LOCATION_PATTERN.fullmatch(location):
        raise CatalogError("Artifact location is invalid.")
    return location


def validate_backup_class(backup_class: str) -> str:
    if backup_class not in BACKUP_CLASSES:
        raise CatalogError("Backup class must be 'config' or 'full'.")
    return backup_class


def _require_text(value: str, *, label: str, required: bool) -> str:
    if not isinstance(value, str) or len(value) > _MAX_TEXT or "\x00" in value:
        raise CatalogError(f"{label} is invalid.")
    value = value.strip()
    if required and not value:
        raise CatalogError(f"{label} is required.")
    return value


def validate_record(record: ArtifactRecord) -> ArtifactRecord:
    if not isinstance(record, ArtifactRecord):
        raise CatalogError("Expected an ArtifactRecord.")
    if not isinstance(record.artifact_id, str) or not _ARTIFACT_ID_PATTERN.fullmatch(record.artifact_id):
        raise CatalogError("Artifact id is invalid.")
    validate_backup_class(record.backup_class)
    validate_location(record.location)
    validate_object_name(record.name)
    if type(record.size) is not int or record.size < 0:
        raise CatalogError("Artifact size is invalid.")
    if not isinstance(record.sha256, str) or not _SHA256_PATTERN.fullmatch(record.sha256):
        raise CatalogError("Artifact sha256 must be 64 lowercase hex characters.")
    if type(record.verified) is not bool or type(record.preserved) is not bool:
        raise CatalogError("Artifact flags must be booleans.")
    if not isinstance(record.change_ids, tuple) or len(record.change_ids) > _MAX_CHANGE_IDS:
        raise CatalogError("Artifact change ids must be a tuple.")
    for change_id in record.change_ids:
        _require_text(change_id, label="Change id", required=True)
    return replace(record, created_at=utc(record.created_at))


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise CatalogError("Backup catalog directory must be a private directory.")


def _ensure_private_database_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CatalogError("Backup catalog database must be a private regular file.") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CatalogError("Backup catalog database must be a private regular file.")
    finally:
        os.close(descriptor)


class ArtifactCatalog:
    """SQLite-backed catalog of backup artifacts, their pins and their deletions."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        _ensure_private_directory(self.path.parent)
        _ensure_private_database_file(self.path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=10.0
        )
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialise_schema()
        except BaseException:
            self._connection.close()
            raise

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> ArtifactCatalog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def _initialise_schema(self) -> None:
        with self._write() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
            ).fetchone()
            if exists is None:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (CATALOG_SCHEMA_VERSION,)
                )
                return
            rows = connection.execute("SELECT version FROM schema_version").fetchall()
            if len(rows) != 1 or type(rows[0][0]) is not int:
                raise CatalogError("Backup catalog schema version is unreadable.")
            if rows[0][0] > CATALOG_SCHEMA_VERSION:
                raise CatalogError("Backup catalog was written by a newer version of the app.")
            if rows[0][0] < 1:
                raise CatalogError("Backup catalog schema version is unsupported.")

    def schema_version(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT version FROM schema_version").fetchone()[0])

    # -- records -------------------------------------------------------------

    _COLUMNS = (
        "artifact_id, backup_class, location, name, created_at, size, sha256, verified, "
        "change_ids, preserved, preserve_reason, preserved_by"
    )

    @staticmethod
    def _record_from_row(row: tuple) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row[0],
            backup_class=row[1],
            location=row[2],
            name=row[3],
            created_at=_timestamp_value(row[4]),
            size=int(row[5]),
            sha256=row[6],
            verified=bool(row[7]),
            change_ids=tuple(json.loads(row[8])),
            preserved=bool(row[9]),
            preserve_reason=row[10],
            preserved_by=row[11],
        )

    def add(self, record: ArtifactRecord, *, now: datetime | None = None) -> ArtifactRecord:
        """Catalogue a new copy. Ids and (location, name) pairs are unique, tombstones included."""

        record = validate_record(record)
        preserve_reason = _require_text(record.preserve_reason, label="Preserve reason", required=False)
        preserved_by = _require_text(record.preserved_by, label="Preserved by", required=False)
        if record.preserved and not preserved_by:
            raise CatalogError("A preserved artifact needs the actor who preserved it.")
        if not record.preserved and (preserve_reason or preserved_by):
            raise CatalogError("Preserve details are only valid on a preserved artifact.")
        record = replace(record, preserve_reason=preserve_reason, preserved_by=preserved_by)
        stamp = _timestamp_text(now or datetime.now(timezone.utc))
        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM tombstones WHERE artifact_id = ?", (record.artifact_id,)
            ).fetchone():
                raise CatalogError("Artifact id was already used by a deleted artifact.")
            try:
                connection.execute(
                    f"INSERT INTO artifacts ({self._COLUMNS}, verified_at, preserved_at, cataloged_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.artifact_id,
                        record.backup_class,
                        record.location,
                        record.name,
                        _timestamp_text(record.created_at),
                        record.size,
                        record.sha256,
                        int(record.verified),
                        json.dumps(list(record.change_ids)),
                        int(record.preserved),
                        record.preserve_reason,
                        record.preserved_by,
                        stamp if record.verified else None,
                        stamp if record.preserved else None,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CatalogError("Artifact id or location/name is already catalogued.") from exc
            if record.preserved:
                connection.execute(
                    "INSERT INTO preserve_events (artifact_id, action, reason, actor, at) "
                    "VALUES (?, 'preserve', ?, ?, ?)",
                    (record.artifact_id, record.preserve_reason, record.preserved_by, stamp),
                )
        return record

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        with self._lock:
            row = self._connection.execute(
                f"SELECT {self._COLUMNS} FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return None if row is None else self._record_from_row(row)

    def list(
        self, *, backup_class: str | None = None, location: str | None = None
    ) -> list[ArtifactRecord]:
        """Live records, oldest first (ties broken by id), optionally filtered."""

        clauses: list[str] = []
        params: list[str] = []
        if backup_class is not None:
            clauses.append("backup_class = ?")
            params.append(validate_backup_class(backup_class))
        if location is not None:
            clauses.append("location = ?")
            params.append(validate_location(location))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {self._COLUMNS} FROM artifacts {where} ORDER BY created_at, artifact_id",
                params,
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def locations(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT location FROM artifacts ORDER BY location"
            ).fetchall()
        return [row[0] for row in rows]

    def _require_live(self, connection: sqlite3.Connection, artifact_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone() is None:
            raise CatalogError("Artifact is not catalogued.")

    def set_preserve(
        self, artifact_id: str, *, reason: str, actor: str, now: datetime | None = None
    ) -> ArtifactRecord:
        """Pin an artifact: grooming never deletes it and it stops counting toward keep-N."""

        reason = _require_text(reason, label="Preserve reason", required=True)
        actor = _require_text(actor, label="Actor", required=True)
        stamp = _timestamp_text(now or datetime.now(timezone.utc))
        with self._write() as connection:
            self._require_live(connection, artifact_id)
            if connection.execute(
                "SELECT deletion_claimed_at FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()[0] is not None:
                raise CatalogError("Artifact is being deleted and can no longer be preserved.")
            connection.execute(
                "UPDATE artifacts SET preserved = 1, preserve_reason = ?, preserved_by = ?, "
                "preserved_at = ? WHERE artifact_id = ?",
                (reason, actor, stamp, artifact_id),
            )
            connection.execute(
                "INSERT INTO preserve_events (artifact_id, action, reason, actor, at) "
                "VALUES (?, 'preserve', ?, ?, ?)",
                (artifact_id, reason, actor, stamp),
            )
        return self.get(artifact_id)  # type: ignore[return-value]

    def clear_preserve(
        self, artifact_id: str, *, actor: str, reason: str = "", now: datetime | None = None
    ) -> ArtifactRecord:
        """Unpin an artifact; it returns to normal grooming."""

        reason = _require_text(reason, label="Unpreserve reason", required=False)
        actor = _require_text(actor, label="Actor", required=True)
        stamp = _timestamp_text(now or datetime.now(timezone.utc))
        with self._write() as connection:
            self._require_live(connection, artifact_id)
            connection.execute(
                "UPDATE artifacts SET preserved = 0, preserve_reason = '', preserved_by = '', "
                "preserved_at = NULL WHERE artifact_id = ?",
                (artifact_id,),
            )
            connection.execute(
                "INSERT INTO preserve_events (artifact_id, action, reason, actor, at) "
                "VALUES (?, 'unpreserve', ?, ?, ?)",
                (artifact_id, reason, actor, stamp),
            )
        return self.get(artifact_id)  # type: ignore[return-value]

    def preserve_history(self, artifact_id: str | None = None) -> list[PreserveEvent]:
        where, params = ("WHERE artifact_id = ?", (artifact_id,)) if artifact_id else ("", ())
        with self._lock:
            rows = self._connection.execute(
                f"SELECT artifact_id, action, reason, actor, at FROM preserve_events {where} "
                "ORDER BY event_id",
                params,
            ).fetchall()
        return [
            PreserveEvent(row[0], row[1], row[2], row[3], _timestamp_value(row[4])) for row in rows
        ]

    def mark_verified(
        self,
        artifact_id: str,
        *,
        sha256: str | None = None,
        size: int | None = None,
        now: datetime | None = None,
    ) -> ArtifactRecord:
        """Record a passed readback check. ``sha256``/``size``, when given, must match the record."""

        stamp = _timestamp_text(now or datetime.now(timezone.utc))
        with self._write() as connection:
            row = connection.execute(
                "SELECT sha256, size FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise CatalogError("Artifact is not catalogued.")
            if sha256 is not None and sha256 != row[0]:
                raise CatalogError("Readback sha256 does not match the catalogued artifact.")
            if size is not None and size != row[1]:
                raise CatalogError("Readback size does not match the catalogued artifact.")
            connection.execute(
                "UPDATE artifacts SET verified = 1, verified_at = ? WHERE artifact_id = ?",
                (stamp, artifact_id),
            )
        return self.get(artifact_id)  # type: ignore[return-value]

    def claim_deletion(
        self,
        expected: ArtifactRecord,
        *,
        actor: str,
        now: datetime | None = None,
        protect_newest_verified: bool = True,
    ) -> None:
        """Atomically check ``expected`` is still current and deletable, and claim it.

        Refused when the record changed (for example it was pinned), is
        preserved, is already claimed, or (by default) is the newest verified
        copy of its class at its location.
        """

        actor = _require_text(actor, label="Actor", required=True)
        stamp = _timestamp_text(now or datetime.now(timezone.utc))
        with self._write() as connection:
            row = connection.execute(
                f"SELECT {self._COLUMNS}, deletion_claimed_at FROM artifacts WHERE artifact_id = ?",
                (expected.artifact_id,),
            ).fetchone()
            if row is None:
                raise CatalogError("Artifact is no longer catalogued.")
            if row[-1] is not None:
                raise CatalogError("Artifact deletion is already claimed.")
            current = self._record_from_row(row[:-1])
            if current != expected:
                raise CatalogError("Artifact changed since the plan was made.")
            if current.preserved:
                raise CatalogError("Artifact is preserved.")
            if protect_newest_verified and _newest_verified_id(
                connection, current.backup_class, current.location
            ) == current.artifact_id:
                raise CatalogError("Artifact is the newest verified copy.")
            connection.execute(
                "UPDATE artifacts SET deletion_claimed_at = ?, deletion_claimed_by = ? "
                "WHERE artifact_id = ?",
                (stamp, actor, current.artifact_id),
            )

    def release_deletion(self, artifact_id: str) -> None:
        """Drop a deletion claim after the object could not be removed."""

        with self._write() as connection:
            connection.execute(
                "UPDATE artifacts SET deletion_claimed_at = NULL, deletion_claimed_by = '' "
                "WHERE artifact_id = ?",
                (artifact_id,),
            )

    def claimed_deletions(self) -> list[tuple[ArtifactRecord, datetime, str]]:
        """Records whose deletion was claimed but not finished (e.g. after a crash)."""

        with self._lock:
            rows = self._connection.execute(
                f"SELECT {self._COLUMNS}, deletion_claimed_at, deletion_claimed_by FROM artifacts "
                "WHERE deletion_claimed_at IS NOT NULL ORDER BY created_at, artifact_id"
            ).fetchall()
        return [(self._record_from_row(row[:-2]), _timestamp_value(row[-2]), row[-1]) for row in rows]

    def is_deletion_claimed(self, artifact_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT deletion_claimed_at FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return row is not None and row[0] is not None

    def record_deletion(
        self,
        artifact_id: str,
        *,
        reason: str,
        actor: str,
        now: datetime | None = None,
        expected: ArtifactRecord | None = None,
    ) -> Tombstone:
        """Move a record into ``tombstones``. With ``expected``, refuse if the record changed."""

        reason = _require_text(reason, label="Deletion reason", required=True)
        actor = _require_text(actor, label="Actor", required=True)
        deleted_at = utc(now or datetime.now(timezone.utc))
        with self._write() as connection:
            row = connection.execute(
                f"SELECT {self._COLUMNS} FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise CatalogError("Artifact is not catalogued.")
            record = self._record_from_row(row)
            if expected is not None and record != expected:
                raise CatalogError("Artifact changed since it was planned for deletion.")
            if record.preserved:
                # Tombstones never hold pinned copies: unpin first, which is audited.
                raise CatalogError("Artifact is preserved; clear the preserve flag before deleting it.")
            connection.execute(
                "INSERT INTO tombstones (artifact_id, backup_class, location, name, created_at, "
                "size, sha256, verified, change_ids, deleted_at, reason, actor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.artifact_id,
                    record.backup_class,
                    record.location,
                    record.name,
                    row[4],
                    record.size,
                    record.sha256,
                    int(record.verified),
                    row[8],
                    _timestamp_text(deleted_at),
                    reason,
                    actor,
                ),
            )
            connection.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        return Tombstone(record=record, deleted_at=deleted_at, reason=reason, actor=actor)

    def tombstones(
        self, *, backup_class: str | None = None, location: str | None = None
    ) -> list[Tombstone]:
        clauses: list[str] = []
        params: list[str] = []
        if backup_class is not None:
            clauses.append("backup_class = ?")
            params.append(validate_backup_class(backup_class))
        if location is not None:
            clauses.append("location = ?")
            params.append(validate_location(location))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT artifact_id, backup_class, location, name, created_at, size, sha256, "
                f"verified, change_ids, deleted_at, reason, actor FROM tombstones {where} "
                "ORDER BY deleted_at, artifact_id",
                params,
            ).fetchall()
        return [
            Tombstone(
                record=ArtifactRecord(
                    artifact_id=row[0],
                    backup_class=row[1],
                    location=row[2],
                    name=row[3],
                    created_at=_timestamp_value(row[4]),
                    size=int(row[5]),
                    sha256=row[6],
                    verified=bool(row[7]),
                    change_ids=tuple(json.loads(row[8])),
                ),
                deleted_at=_timestamp_value(row[9]),
                reason=row[10],
                actor=row[11],
            )
            for row in rows
        ]
