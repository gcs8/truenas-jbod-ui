from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, TypeAlias

from pydantic import ValidationError

from app.models.domain import ManualMapping
from app.services.profile_registry import ENCLOSURE_SUB_VIEW_PROFILE_IDS

_DRAWER_SUB_PROFILE_IDS = frozenset(
    sub_profile_id
    for sub_profile_ids in ENCLOSURE_SUB_VIEW_PROFILE_IDS.values()
    for sub_profile_id in sub_profile_ids
)

Identity: TypeAlias = tuple[str | None, str | None, int]
ScopeIdentity: TypeAlias = tuple[str | None, int]


def resolve_physical_mapping_scope(enclosure_id: str | None) -> str | None:
    if not enclosure_id:
        return enclosure_id
    physical_id, separator, profile_id = enclosure_id.rpartition("::")
    _parent_id, parent_separator, parent_profile_id = physical_id.rpartition("::")
    if (
        separator
        and physical_id
        and profile_id in _DRAWER_SUB_PROFILE_IDS
        and not (parent_separator and parent_profile_id in _DRAWER_SUB_PROFILE_IDS)
    ):
        return physical_id
    return enclosure_id


class MappingRevisionConflict(RuntimeError):
    public_detail = "Mapping scope revision changed before this write."

    def __init__(self, current_revision: str) -> None:
        super().__init__(self.public_detail)
        self.current_revision = current_revision


class MappingImportDigestMismatch(RuntimeError):
    public_detail = "Mapping import digest does not match the confirmed preview."

    def __init__(self, current_revision: str, current_import_digest: str) -> None:
        super().__init__(self.public_detail)
        self.current_revision = current_revision
        self.current_import_digest = current_import_digest


class MappingScopeConflict(RuntimeError):
    public_detail = "Conflicting mapping rows exist for the same physical slot."

    def __init__(self) -> None:
        super().__init__(self.public_detail)


class MappingDurabilityError(RuntimeError):
    """A complete replacement is visible but its durability is indeterminate."""

    public_detail = "Mapping update is visible but durability could not be confirmed."

    def __init__(self) -> None:
        super().__init__(self.public_detail)


@dataclass(frozen=True)
class _ClassifiedRow:
    key: str
    stored: ManualMapping
    identity: Identity
    canonical: ManualMapping


@dataclass(frozen=True)
class _ClassifiedStore:
    version: int
    entries: dict[str, ManualMapping]
    rows: tuple[_ClassifiedRow, ...]
    mappings: dict[Identity, ManualMapping]


@dataclass(frozen=True)
class _ScopedStoreSnapshot:
    state: _ClassifiedStore
    invalid_rows: tuple[tuple[str, ManualMapping], ...]
    conflicting_identities: frozenset[Identity]


@dataclass(frozen=True)
class _TempFileIdentity:
    device: int
    inode: int


class _VersionedEntries(dict[str, ManualMapping]):
    __slots__ = ("__store_version",)

    def __init__(
        self,
        entries: Mapping[str, ManualMapping],
        *,
        store_version: int,
    ) -> None:
        super().__init__(entries)
        self.__store_version = store_version

    @property
    def store_version(self) -> int:
        return self.__store_version


class MappingStore:
    """Persist slot-to-disk calibration in a small JSON file on a bind mount."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # Kept as the exact version-1 encoder for historical fixtures and validation.
    def _slot_key(self, system_id: str | None, enclosure_id: str | None, slot: int) -> str:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        return f"{system_id or 'default_system'}:{physical_enclosure_id or 'default'}:{slot}"

    @staticmethod
    def _encode_v2_key(
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        if not (system_id is None or isinstance(system_id, str)):
            raise MappingScopeConflict()
        if not (enclosure_id is None or isinstance(enclosure_id, str)):
            raise MappingScopeConflict()
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise MappingScopeConflict()
        return "v2:" + json.dumps(
            [system_id, enclosure_id, slot],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    @classmethod
    def _decode_v2_key(cls, key: str) -> Identity:
        if not isinstance(key, str) or not key.startswith("v2:"):
            raise MappingScopeConflict()
        encoded = key[3:]
        try:
            decoded = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeError):
            raise MappingScopeConflict() from None
        if not isinstance(decoded, list) or len(decoded) != 3:
            raise MappingScopeConflict()
        system_id, enclosure_id, slot = decoded
        if not (system_id is None or isinstance(system_id, str)):
            raise MappingScopeConflict()
        if not (enclosure_id is None or isinstance(enclosure_id, str)):
            raise MappingScopeConflict()
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise MappingScopeConflict()
        identity = (system_id, enclosure_id, slot)
        if cls._encode_v2_key(*identity) != key:
            raise MappingScopeConflict()
        return identity

    @staticmethod
    def _canonical_mapping(mapping: ManualMapping) -> ManualMapping:
        return mapping.model_copy(
            update={"enclosure_id": resolve_physical_mapping_scope(mapping.enclosure_id)}
        )

    @staticmethod
    def _drawer_alias_enclosure_ids(enclosure_id: str | None) -> tuple[str, ...]:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        if not physical_enclosure_id:
            return ()
        return tuple(
            f"{physical_enclosure_id}::{profile_id}"
            for profile_id in sorted(_DRAWER_SUB_PROFILE_IDS)
        )

    def _v1_physical_keys(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> tuple[str, ...]:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        prefix = system_id or "default_system"
        keys = [f"{prefix}:{physical_enclosure_id or 'default'}:{slot}"]
        keys.extend(
            f"{prefix}:{alias_id}:{slot}"
            for alias_id in self._drawer_alias_enclosure_ids(physical_enclosure_id)
        )
        return tuple(dict.fromkeys(keys))

    def _v1_legacy_keys(self, enclosure_id: str | None, slot: int) -> tuple[str, ...]:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        keys = [f"{physical_enclosure_id or 'default'}:{slot}"]
        keys.extend(
            f"{alias_id}:{slot}"
            for alias_id in self._drawer_alias_enclosure_ids(physical_enclosure_id)
        )
        return tuple(dict.fromkeys(keys))

    @classmethod
    def _semantic_mapping(cls, mapping: ManualMapping) -> dict[str, Any]:
        mapping = cls._canonical_mapping(mapping)
        return {
            "enclosure_id": mapping.enclosure_id,
            "slot": mapping.slot,
            "serial": mapping.serial,
            "device_name": mapping.device_name,
            "gptid": mapping.gptid,
            "notes": mapping.notes,
        }

    @staticmethod
    def _digest(payload: Any) -> str:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_raw_model(raw: Any) -> ManualMapping:
        if not isinstance(raw, dict):
            raise MappingScopeConflict()
        if isinstance(raw.get("slot"), bool) or not isinstance(raw.get("slot"), int):
            raise MappingScopeConflict()
        for field in ("system_id", "enclosure_id"):
            value = raw.get(field)
            if value is not None and not isinstance(value, str):
                raise MappingScopeConflict()
        try:
            return ManualMapping.model_validate(raw)
        except ValidationError:
            raise MappingScopeConflict() from None

    @staticmethod
    def _reject_duplicate_object_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MappingScopeConflict()
            result[key] = value
        return result

    def _read_document(
        self,
        *,
        strict: bool = True,
        tolerate_invalid_models: bool = False,
    ) -> tuple[int, dict[str, ManualMapping]]:
        """Read once; authoritative callers fail closed, legacy display may degrade."""
        try:
            raw = self._read_source_bytes()
        except OSError:
            if strict:
                raise
            return 1, {}
        if raw is None:
            return 2, {}
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=self._reject_duplicate_object_keys,
            )
        except (UnicodeError, json.JSONDecodeError):
            if strict:
                raise MappingScopeConflict() from None
            return 1, {}
        if not isinstance(payload, dict):
            if strict:
                raise MappingScopeConflict()
            return 1, {}
        version = payload.get("version", 1)
        if isinstance(version, bool) or version not in (1, 2):
            raise MappingScopeConflict()
        raw_mappings = payload.get("slot_mappings")
        if not isinstance(raw_mappings, dict):
            if strict:
                raise MappingScopeConflict()
            if version == 1:
                return version, {}
            raise MappingScopeConflict()
        entries: dict[str, ManualMapping] = {}
        for key, value in raw_mappings.items():
            if not isinstance(key, str):
                raise MappingScopeConflict()
            try:
                entries[key] = self._validate_raw_model(value)
            except MappingScopeConflict:
                if tolerate_invalid_models and version == 1:
                    return version, {}
                raise
        return version, entries

    def _read_source_bytes(self) -> bytes | None:
        try:
            observed = os.lstat(self.file_path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(observed.st_mode):
            raise OSError("Configured mapping source is not a regular file.")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.file_path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != observed.st_dev
                or opened.st_ino != observed.st_ino
            ):
                raise OSError("Configured mapping source identity changed during open.")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _classify_v1(self, key: str, mapping: ManualMapping) -> tuple[Identity, ManualMapping]:
        canonical = self._canonical_mapping(mapping)
        system_id = canonical.system_id
        enclosure_id = canonical.enclosure_id
        slot = canonical.slot
        if system_id is not None:
            accepted = {
                *self._v1_physical_keys(system_id, enclosure_id, slot),
                *self._v1_legacy_keys(enclosure_id, slot),
            }
            if key not in accepted:
                raise MappingScopeConflict()
            return (system_id, enclosure_id, slot), canonical

        legacy_keys = set(self._v1_legacy_keys(enclosure_id, slot))
        if key in legacy_keys:
            return (None, enclosure_id, slot), canonical

        enclosure_tokens = [enclosure_id or "default"]
        enclosure_tokens.extend(self._drawer_alias_enclosure_ids(enclosure_id))
        owners: set[str | None] = set()
        for token in dict.fromkeys(enclosure_tokens):
            suffix = f":{token}:{slot}"
            if not key.endswith(suffix):
                continue
            owner = key[: -len(suffix)]
            if owner:
                owners.add(None if owner == "default_system" else owner)
        if len(owners) != 1:
            raise MappingScopeConflict()
        owner = next(iter(owners))
        canonical = canonical.model_copy(update={"system_id": owner})
        return (owner, enclosure_id, slot), canonical

    def _classify_row(
        self,
        version: int,
        key: str,
        mapping: ManualMapping,
    ) -> _ClassifiedRow:
        if version == 1:
            identity, canonical = self._classify_v1(key, mapping)
        elif version == 2:
            identity = self._decode_v2_key(key)
            canonical = self._canonical_mapping(mapping)
            model_identity = (
                canonical.system_id,
                canonical.enclosure_id,
                canonical.slot,
            )
            if identity != model_identity:
                raise MappingScopeConflict()
        else:
            raise MappingScopeConflict()
        return _ClassifiedRow(key, mapping, identity, canonical)

    def _classify_entries(
        self,
        version: int,
        entries: Mapping[str, ManualMapping],
    ) -> _ClassifiedStore:
        rows = tuple(
            self._classify_row(version, key, mapping)
            for key, mapping in entries.items()
        )
        grouped: dict[Identity, list[_ClassifiedRow]] = {}
        for row in rows:
            grouped.setdefault(row.identity, []).append(row)
        mappings: dict[Identity, ManualMapping] = {}
        for identity, identity_rows in grouped.items():
            semantic_rows = {
                self._digest(self._semantic_mapping(row.canonical))
                for row in identity_rows
            }
            if len(semantic_rows) != 1:
                raise MappingScopeConflict()
            winner = max(
                identity_rows,
                key=lambda row: (
                    row.key == self._slot_key(*identity),
                    row.key,
                ),
            )
            mappings[identity] = winner.canonical
        return _ClassifiedStore(version, dict(entries), rows, mappings)

    def _load_state(self) -> _ClassifiedStore:
        version, entries = self._read_document()
        return self._classify_entries(version, entries)

    def _v1_resolvable_keys(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> set[str]:
        return {
            *self._v1_physical_keys(system_id, enclosure_id, slot),
            *self._v1_physical_keys(system_id, None, slot),
            *self._v1_legacy_keys(enclosure_id, slot),
            *self._v1_legacy_keys(None, slot),
        }

    def _invalid_row_is_relevant(
        self,
        version: int,
        key: str,
        mapping: ManualMapping,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int | None,
    ) -> bool:
        canonical = self._canonical_mapping(mapping)
        model_system_matches = (
            canonical.system_id in {None, system_id}
            if system_id is not None
            else canonical.system_id is None
        )
        model_matches = (
            model_system_matches
            and (slot is None or canonical.slot == slot)
            and (
                enclosure_id is None
                or canonical.enclosure_id in {None, enclosure_id}
            )
        )
        if model_matches:
            return True
        candidate_slot = canonical.slot if slot is None else slot
        if version == 1:
            return key in self._v1_resolvable_keys(
                system_id, enclosure_id, candidate_slot
            )
        try:
            key_identity = self._decode_v2_key(key)
        except MappingScopeConflict:
            return False
        return (
            self._identity_matches_system(key_identity, system_id)
            and (slot is None or key_identity[2] == slot)
            and (enclosure_id is None or key_identity[1] in {None, enclosure_id})
        )

    def _load_scoped_snapshot(self) -> _ScopedStoreSnapshot:
        """Classify once; defer conflict rejection to each exact requested target.

        Revision batches hold the store lock and keep this snapshot local to
        one call. Unlike mutation validation, unrelated bad rows must not
        prevent scoped revision issuance.
        """
        version, entries = self._read_document()
        rows: list[_ClassifiedRow] = []
        invalid_rows: list[tuple[str, ManualMapping]] = []
        for key, mapping in entries.items():
            try:
                rows.append(self._classify_row(version, key, mapping))
            except MappingScopeConflict:
                invalid_rows.append((key, mapping))

        grouped: dict[Identity, list[_ClassifiedRow]] = {}
        for row in rows:
            grouped.setdefault(row.identity, []).append(row)
        mappings: dict[Identity, ManualMapping] = {}
        conflicting_identities: set[Identity] = set()
        for identity, identity_rows in grouped.items():
            semantic_rows = {
                self._digest(self._semantic_mapping(row.canonical))
                for row in identity_rows
            }
            if len(semantic_rows) != 1:
                conflicting_identities.add(identity)
            winner = max(identity_rows, key=lambda row: row.key)
            mappings[identity] = winner.canonical
        return _ScopedStoreSnapshot(
            _ClassifiedStore(version, entries, tuple(rows), mappings),
            tuple(invalid_rows),
            frozenset(conflicting_identities),
        )

    def _load_state_for_scope(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        *,
        slot: int | None = None,
        snapshot: _ScopedStoreSnapshot | None = None,
    ) -> _ClassifiedStore:
        if snapshot is None:
            snapshot = self._load_scoped_snapshot()
        state = snapshot.state
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        for key, mapping in snapshot.invalid_rows:
            if self._invalid_row_is_relevant(
                state.version, key, mapping, system_id, enclosure_id, slot
            ):
                raise MappingScopeConflict()
        for identity in snapshot.conflicting_identities:
            if (
                self._identity_matches_system(identity, system_id)
                and (slot is None or identity[2] == slot)
                and (enclosure_id is None or identity[1] in {None, enclosure_id})
            ):
                raise MappingScopeConflict()

        selected_rows = [
            row
            for row in state.rows
            if row.identity[0] in {None, system_id}
            and (slot is None or row.identity[2] == slot)
            and (enclosure_id is None or row.identity[1] == enclosure_id)
        ]
        for legacy_row in selected_rows:
            if legacy_row.identity[0] is not None:
                continue
            if legacy_row.stored.enclosure_id == legacy_row.canonical.enclosure_id:
                continue
            for scoped_row in selected_rows:
                if scoped_row.identity[0] != system_id:
                    continue
                if scoped_row.identity[1:] != legacy_row.identity[1:]:
                    continue
                if self._semantic_mapping(scoped_row.canonical) != self._semantic_mapping(
                    legacy_row.canonical
                ):
                    raise MappingScopeConflict()
        return state

    def _state_from_entries(
        self,
        entries: Mapping[str, ManualMapping],
    ) -> _ClassifiedStore:
        if isinstance(entries, _VersionedEntries):
            return self._classify_entries(entries.store_version, entries)
        if not entries:
            return self._classify_entries(2, entries)
        prefixes = {key.startswith("v2:") for key in entries}
        if len(prefixes) != 1:
            raise MappingScopeConflict()
        return self._classify_entries(2 if True in prefixes else 1, entries)

    def load_all(self) -> dict[str, ManualMapping]:
        """Load for historical read-only display, tolerating corrupt v1-era stores."""
        version, entries = self._read_document(
            strict=False,
            tolerate_invalid_models=True,
        )
        if version == 2:
            self._classify_entries(version, entries)
        return _VersionedEntries(entries, store_version=version)

    @staticmethod
    def _query_identities(
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        allow_legacy_fallback: bool,
    ) -> tuple[Identity, ...]:
        identities: list[Identity] = [(system_id, enclosure_id, slot)]
        if allow_legacy_fallback:
            identities.extend(
                (
                    (system_id, None, slot),
                    (None, enclosure_id, slot),
                    (None, None, slot),
                )
            )
        return tuple(dict.fromkeys(identities))

    def get_mapping(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        allow_legacy_fallback: bool = False,
        loaded_entries: Mapping[str, ManualMapping] | None = None,
    ) -> ManualMapping | None:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        state = (
            self._load_state_for_scope(system_id, enclosure_id, slot=slot)
            if loaded_entries is None
            else self._state_from_entries(loaded_entries)
        )
        for identity in self._query_identities(
            system_id, enclosure_id, slot, allow_legacy_fallback
        ):
            mapping = state.mappings.get(identity)
            if mapping is not None:
                return self._canonical_mapping(mapping)
        return None

    def has_legacy_only_mapping(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        loaded_entries: Mapping[str, ManualMapping] | None = None,
    ) -> bool:
        state_entries = self.load_all() if loaded_entries is None else loaded_entries
        if self.get_mapping(
            system_id,
            enclosure_id,
            slot,
            loaded_entries=state_entries,
        ) is not None:
            return False
        return self.get_mapping(
            system_id,
            enclosure_id,
            slot,
            allow_legacy_fallback=True,
            loaded_entries=state_entries,
        ) is not None

    @staticmethod
    def _identity_matches_system(identity: Identity, system_id: str | None) -> bool:
        if system_id is None:
            return identity[0] is None
        return identity[0] in {None, system_id}

    def _scope_entries(
        self,
        state: _ClassifiedStore,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[ScopeIdentity, ManualMapping]:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        selected: dict[ScopeIdentity, tuple[int, ManualMapping]] = {}
        for identity, mapping in state.mappings.items():
            if not self._identity_matches_system(identity, system_id):
                continue
            if enclosure_id is not None and identity[1] != enclosure_id:
                continue
            scope_identity = (identity[1], identity[2])
            rank = 1 if identity[0] == system_id else 0
            existing = selected.get(scope_identity)
            if existing is None or rank > existing[0]:
                selected[scope_identity] = (rank, mapping)
        return {identity: ranked[1] for identity, ranked in selected.items()}

    def count_for_system(self, system_id: str | None) -> int:
        if system_id is None:
            state = self._load_state()
            return len(state.mappings)
        state = self._load_state_for_scope(system_id, None)
        return len(self._scope_entries(state, system_id, None))

    def list_mappings(
        self,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> list[ManualMapping]:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        if system_id is not None:
            state = self._load_state_for_scope(system_id, enclosure_id)
            selected = self._scope_entries(state, system_id, enclosure_id)
            return [
                selected[identity]
                for identity in sorted(
                    selected,
                    key=lambda item: (item[0] or "", item[1]),
                )
            ]
        state = self._load_state()
        mappings = [
            mapping
            for identity, mapping in state.mappings.items()
            if enclosure_id is None or identity[1] == enclosure_id
        ]
        return sorted(
            mappings,
            key=lambda mapping: (
                mapping.system_id or "",
                mapping.enclosure_id or "",
                mapping.slot,
            ),
        )

    def _normalize_incoming(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> dict[ScopeIdentity, ManualMapping]:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        normalized: dict[ScopeIdentity, ManualMapping] = {}
        for mapping in mappings:
            mapping_enclosure_id = resolve_physical_mapping_scope(mapping.enclosure_id)
            scoped = mapping.model_copy(
                update={
                    "system_id": system_id,
                    "enclosure_id": (
                        enclosure_id
                        if enclosure_id is not None
                        else mapping_enclosure_id
                    ),
                }
            )
            identity = (scoped.enclosure_id, scoped.slot)
            if identity in normalized:
                raise ValueError(
                    f"Duplicate mapping for enclosure {identity[0] or 'default'} "
                    f"slot {identity[1]}."
                )
            normalized[identity] = scoped
        return normalized

    @staticmethod
    def _identity_payload(identity: ScopeIdentity) -> dict[str, Any]:
        return {"enclosure_id": identity[0], "slot": identity[1]}

    def _mapping_value_payload(self, mapping: ManualMapping) -> dict[str, Any]:
        return {
            key: value
            for key, value in self._semantic_mapping(mapping).items()
            if key not in {"enclosure_id", "slot"}
        }

    @staticmethod
    def _row_payload(row: _ClassifiedRow) -> dict[str, Any]:
        return {
            "key": row.key,
            "identity": list(row.identity),
            "model": row.stored.model_dump(mode="json"),
        }

    def _rows_matching(
        self,
        state: _ClassifiedStore,
        predicate: Any,
    ) -> list[dict[str, Any]]:
        return [
            self._row_payload(row)
            for row in sorted(state.rows, key=lambda item: item.key)
            if predicate(row.identity)
        ]

    def _import_identity_selected(
        self,
        identity: Identity,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> bool:
        if not self._identity_matches_system(identity, system_id):
            return False
        return enclosure_id is None or identity[1] == enclosure_id

    def _replace_identity_selected(
        self,
        identity: Identity,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> bool:
        if not self._identity_matches_system(identity, system_id):
            return False
        return enclosure_id is None or identity[1] in {None, enclosure_id}

    def _resolvable_identity_selected(
        self,
        identity: Identity,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> bool:
        return identity in self._query_identities(
            system_id, enclosure_id, slot, True
        )

    def _scope_revision_from_state(
        self,
        state: _ClassifiedStore,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        rows = self._rows_matching(
            state,
            lambda identity: self._import_identity_selected(
                identity, system_id, enclosure_id
            ),
        )
        return self._digest(
            {
                "version": state.version,
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "rows": rows,
            }
        )

    def _resolvable_revision_from_state(
        self,
        state: _ClassifiedStore,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        rows = self._rows_matching(
            state,
            lambda identity: self._resolvable_identity_selected(
                identity, system_id, enclosure_id, slot
            ),
        )
        return self._digest(
            {
                "version": state.version,
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "slot": slot,
                "rows": rows,
            }
        )

    def _save_revision_from_state(
        self,
        state: _ClassifiedStore,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        scope_revision: str | None = None,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        if scope_revision is None:
            scope_revision = self._scope_revision_from_state(
                state, system_id, enclosure_id
            )
        return self._digest(
            {
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "slot": slot,
                "scope_revision": scope_revision,
                "resolvable_revision": self._resolvable_revision_from_state(
                    state, system_id, enclosure_id, slot
                ),
            }
        )

    def _clear_revision_from_state(
        self,
        state: _ClassifiedStore,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        effective = None
        for identity in self._query_identities(system_id, enclosure_id, slot, True):
            if identity in state.mappings:
                effective = state.mappings[identity]
                break
        effective_scope = effective.enclosure_id if effective is not None else enclosure_id
        return self._digest(
            {
                "enclosure_id": enclosure_id,
                "slot": slot,
                "effective_scope": effective_scope,
                "effective_revision": self._scope_revision_from_state(
                    state, system_id, effective_scope
                ),
                "resolvable_revision": self._resolvable_revision_from_state(
                    state, system_id, enclosure_id, slot
                ),
            }
        )

    def _preview_from_state(
        self,
        state: _ClassifiedStore,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> tuple[dict[str, Any], dict[ScopeIdentity, ManualMapping]]:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        current_scope = self._scope_entries(state, system_id, enclosure_id)
        incoming_scope = self._normalize_incoming(system_id, enclosure_id, mappings)
        revision = self._scope_revision_from_state(state, system_id, enclosure_id)
        additions: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        removals: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        all_identities = sorted(
            set(current_scope) | set(incoming_scope),
            key=lambda item: (item[0] or "", item[1]),
        )
        for identity in all_identities:
            current_mapping = current_scope.get(identity)
            incoming_mapping = incoming_scope.get(identity)
            target = self._identity_payload(identity)
            if current_mapping is None:
                assert incoming_mapping is not None
                additions.append(
                    {**target, "incoming": self._mapping_value_payload(incoming_mapping)}
                )
            elif incoming_mapping is None:
                removals.append(
                    {**target, "current": self._mapping_value_payload(current_mapping)}
                )
            elif self._semantic_mapping(current_mapping) == self._semantic_mapping(
                incoming_mapping
            ):
                unchanged.append(target)
            else:
                current_values = self._mapping_value_payload(current_mapping)
                incoming_values = self._mapping_value_payload(incoming_mapping)
                updates.append(
                    {
                        **target,
                        "changes": {
                            field: {
                                "from": current_values[field],
                                "to": incoming_values[field],
                            }
                            for field in sorted(current_values)
                            if current_values[field] != incoming_values[field]
                        },
                    }
                )
        incoming_payload = [
            self._semantic_mapping(incoming_scope[identity])
            for identity in sorted(
                incoming_scope, key=lambda item: (item[0] or "", item[1])
            )
        ]
        preview = {
            "revision": revision,
            "import_digest": self._digest(
                {
                    "revision": revision,
                    "system_id": system_id,
                    "enclosure_id": enclosure_id,
                    "mappings": incoming_payload,
                }
            ),
            "additions": additions,
            "updates": updates,
            "removals": removals,
            "unchanged": unchanged,
        }
        return preview, incoming_scope

    def preview_replace_mappings(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> dict[str, Any]:
        with self._lock:
            preview, _ = self._preview_from_state(
                self._load_state_for_scope(system_id, enclosure_id),
                system_id,
                enclosure_id,
                mappings,
            )
            return preview

    def scope_revision(
        self,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> str:
        with self._lock:
            return self._scope_revision_from_state(
                self._load_state_for_scope(system_id, enclosure_id),
                system_id,
                enclosure_id,
            )

    def clear_revision(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        with self._lock:
            return self._clear_revision_from_state(
                self._load_state_for_scope(system_id, enclosure_id, slot=slot),
                system_id,
                enclosure_id,
                slot,
            )

    def save_revision(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        with self._lock:
            return self._save_revision_from_state(
                self._load_state_for_scope(system_id, enclosure_id, slot=slot),
                system_id,
                enclosure_id,
                slot,
            )

    def save_revisions(
        self,
        system_id: str | None,
        targets: list[tuple[str | None, int]],
    ) -> dict[tuple[str | None, int], str]:
        with self._lock:
            if not targets:
                return {}
            snapshot = self._load_scoped_snapshot()
            canonical_targets = {
                target: (resolve_physical_mapping_scope(target[0]), target[1])
                for target in targets
            }
            return {
                target: self._save_revision_from_state(
                    state := self._load_state_for_scope(
                        system_id, canonical[0], slot=canonical[1], snapshot=snapshot
                    ),
                    system_id,
                    canonical[0],
                    canonical[1],
                    scope_revision=self._scope_revision_from_state(
                        state, system_id, canonical[0]
                    ),
                )
                for target, canonical in canonical_targets.items()
            }

    def clear_revisions(
        self,
        system_id: str | None,
        targets: list[tuple[str | None, int]],
    ) -> dict[tuple[str | None, int], str]:
        with self._lock:
            if not targets:
                return {}
            snapshot = self._load_scoped_snapshot()
            return {
                target: self._clear_revision_from_state(
                    self._load_state_for_scope(
                        system_id,
                        resolve_physical_mapping_scope(target[0]),
                        slot=target[1],
                        snapshot=snapshot,
                    ),
                    system_id,
                    resolve_physical_mapping_scope(target[0]),
                    target[1],
                )
                for target in targets
            }

    def save_mapping(
        self,
        mapping: ManualMapping,
        *,
        expected_revision: str | None = None,
    ) -> ManualMapping:
        with self._lock:
            state = self._load_state()
            mapping = self._canonical_mapping(mapping)
            if expected_revision is not None:
                current_revision = self._save_revision_from_state(
                    state,
                    mapping.system_id,
                    mapping.enclosure_id,
                    mapping.slot,
                )
                if current_revision != expected_revision:
                    raise MappingRevisionConflict(current_revision)
            saved = mapping.model_copy(
                update={"updated_at": datetime.now(timezone.utc)}
            )
            current = dict(state.mappings)
            for identity in tuple(current):
                if self._resolvable_identity_selected(
                    identity,
                    mapping.system_id,
                    mapping.enclosure_id,
                    mapping.slot,
                ):
                    current.pop(identity)
            identity = (saved.system_id, saved.enclosure_id, saved.slot)
            current[identity] = saved
            self._commit_v2(current)
            return saved

    def clear_mapping(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        expected_revision: str | None = None,
    ) -> bool:
        with self._lock:
            state = self._load_state()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            if expected_revision is not None:
                current_revision = self._clear_revision_from_state(
                    state, system_id, enclosure_id, slot
                )
                if current_revision != expected_revision:
                    raise MappingRevisionConflict(current_revision)
            effective = next(
                (
                    identity
                    for identity in self._query_identities(
                        system_id, enclosure_id, slot, True
                    )
                    if identity in state.mappings
                ),
                None,
            )
            if effective is None:
                return False
            current = dict(state.mappings)
            for identity in tuple(current):
                if self._resolvable_identity_selected(
                    identity, system_id, enclosure_id, slot
                ):
                    current.pop(identity)
            self._commit_v2(current)
            return True

    def replace_mappings(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> int:
        with self._lock:
            state = self._load_state()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            incoming = self._normalize_incoming(system_id, enclosure_id, mappings)
            current = {
                identity: mapping
                for identity, mapping in state.mappings.items()
                if not self._replace_identity_selected(
                    identity, system_id, enclosure_id
                )
            }
            now = datetime.now(timezone.utc)
            for mapping in incoming.values():
                saved = mapping.model_copy(update={"updated_at": now})
                current[(saved.system_id, saved.enclosure_id, saved.slot)] = saved
            self._commit_v2(current)
            return len(incoming)

    def apply_mapping_import(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
        *,
        expected_revision: str,
        import_digest: str,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load_state()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            preview, incoming = self._preview_from_state(
                state, system_id, enclosure_id, mappings
            )
            if preview["revision"] != expected_revision:
                raise MappingRevisionConflict(preview["revision"])
            if preview["import_digest"] != import_digest:
                raise MappingImportDigestMismatch(
                    preview["revision"], preview["import_digest"]
                )
            current = {
                identity: mapping
                for identity, mapping in state.mappings.items()
                if not self._import_identity_selected(
                    identity, system_id, enclosure_id
                )
            }
            now = datetime.now(timezone.utc)
            for mapping in incoming.values():
                saved = mapping.model_copy(update={"updated_at": now})
                current[(saved.system_id, saved.enclosure_id, saved.slot)] = saved
            self._commit_v2(current)
            final_state = self._classify_entries(
                2,
                {
                    self._encode_v2_key(*identity): mapping
                    for identity, mapping in current.items()
                },
            )
            final_preview, _ = self._preview_from_state(
                final_state, system_id, enclosure_id, list(incoming.values())
            )
            return {
                "saved_count": len(incoming),
                "revision": final_preview["revision"],
                "preview": preview,
            }

    def _serialize_v2(self, mappings: Mapping[Identity, ManualMapping]) -> bytes:
        slot_mappings: dict[str, dict[str, Any]] = {}
        for identity in sorted(
            mappings,
            key=lambda item: (item[0] or "", item[1] or "", item[2]),
        ):
            mapping = self._canonical_mapping(mappings[identity]).model_copy(
                update={"system_id": identity[0], "enclosure_id": identity[1]}
            )
            if (mapping.system_id, mapping.enclosure_id, mapping.slot) != identity:
                raise MappingScopeConflict()
            slot_mappings[self._encode_v2_key(*identity)] = mapping.model_dump(
                mode="json"
            )
        payload = {
            "slot_mappings": slot_mappings,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "version": 2,
        }
        data = (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        decoded = json.loads(data.decode("utf-8"))
        if (
            json.dumps(
                decoded,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
            != data
        ):
            raise MappingScopeConflict()
        entries = {
            key: self._validate_raw_model(value)
            for key, value in decoded["slot_mappings"].items()
        }
        self._classify_entries(2, entries)
        return data

    @staticmethod
    def _unlink_owned_temp_file(
        temp_path: Path,
        identity: _TempFileIdentity,
    ) -> None:
        try:
            current = os.lstat(temp_path)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_dev != identity.device
                or current.st_ino != identity.inode
            ):
                return
            temp_path.unlink()
        except OSError:
            pass

    def _create_temp_file(self) -> tuple[Path, int, _TempFileIdentity]:
        for _attempt in range(128):
            temp_path = self.file_path.parent / (
                f"{self.file_path.name}.{secrets.token_hex(16)}.tmp"
            )
            try:
                descriptor = os.open(
                    temp_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o660,
                )
            except FileExistsError:
                continue
            temp_stat: os.stat_result | None = None
            temp_identity: _TempFileIdentity | None = None
            try:
                os.fchmod(descriptor, 0o660)
                temp_stat = os.fstat(descriptor)
                if not stat.S_ISREG(temp_stat.st_mode):
                    raise OSError("Mapping temporary path is not a regular file.")
                temp_identity = _TempFileIdentity(
                    device=temp_stat.st_dev,
                    inode=temp_stat.st_ino,
                )
                if stat.S_IMODE(temp_stat.st_mode) != 0o660:
                    raise OSError("Mapping temporary file mode is not 0660.")
            except Exception:
                if temp_stat is None:
                    try:
                        temp_stat = os.fstat(descriptor)
                        if stat.S_ISREG(temp_stat.st_mode):
                            temp_identity = _TempFileIdentity(
                                device=temp_stat.st_dev,
                                inode=temp_stat.st_ino,
                            )
                    except OSError:
                        pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                if temp_identity is not None:
                    self._unlink_owned_temp_file(temp_path, temp_identity)
                raise
            assert temp_identity is not None
            return temp_path, descriptor, temp_identity
        raise FileExistsError("Could not allocate a unique mapping temporary file.")

    @staticmethod
    def _write_temp_bytes(handle: BinaryIO, data: bytes) -> int:
        total = 0
        while total < len(data):
            written = handle.write(data[total:])
            if (
                isinstance(written, bool)
                or not isinstance(written, int)
                or written <= 0
                or written > len(data) - total
            ):
                raise OSError("Could not write the complete mapping temporary file.")
            total += written
        return total

    @staticmethod
    def _flush_temp_file(handle: BinaryIO) -> None:
        handle.flush()

    @staticmethod
    def _fsync_temp_file(handle: BinaryIO) -> None:
        os.fsync(handle.fileno())

    def _write_temp_file(self, descriptor: int, data: bytes) -> None:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            written = self._write_temp_bytes(handle, data)
            if written != len(data):
                raise OSError("Mapping temporary file write was incomplete.")
            self._flush_temp_file(handle)
            self._fsync_temp_file(handle)

    def _replace_temp_file(self, temp_path: Path) -> None:
        os.replace(temp_path, self.file_path)

    def _fsync_parent_directory(self) -> None:
        descriptor = os.open(self.file_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _validate_published_bytes(self, expected: bytes) -> None:
        actual = self.file_path.read_bytes()
        if actual != expected:
            raise OSError("Published mapping bytes differ from the replacement.")
        payload = json.loads(actual.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 2:
            raise OSError("Published mapping generation is invalid.")
        raw_mappings = payload.get("slot_mappings")
        if not isinstance(raw_mappings, dict):
            raise OSError("Published mapping generation is invalid.")
        entries = {
            key: self._validate_raw_model(value)
            for key, value in raw_mappings.items()
        }
        self._classify_entries(2, entries)

    def _commit_v2(self, mappings: Mapping[Identity, ManualMapping]) -> None:
        data = self._serialize_v2(mappings)
        temp_path: Path | None = None
        descriptor: int | None = None
        temp_identity: _TempFileIdentity | None = None
        replaced = False
        try:
            temp_path, descriptor, temp_identity = self._create_temp_file()
            self._write_temp_file(descriptor, data)
            os.close(descriptor)
            descriptor = None
            self._replace_temp_file(temp_path)
            replaced = True
            self._fsync_parent_directory()
            self._validate_published_bytes(data)
        except Exception as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if (
                not replaced
                and temp_path is not None
                and temp_identity is not None
            ):
                self._unlink_owned_temp_file(temp_path, temp_identity)
            if replaced:
                raise MappingDurabilityError() from exc
            raise

    def _write(self, mappings: dict[str, ManualMapping]) -> None:
        """Write an explicit version-1 fixture; production mutations use v2."""
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "slot_mappings": {
                key: value.model_dump(mode="json")
                for key, value in mappings.items()
            },
        }
        self.file_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
