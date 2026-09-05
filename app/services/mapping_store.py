from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.domain import ManualMapping
from app.services.profile_registry import ENCLOSURE_SUB_VIEW_PROFILE_IDS

_DRAWER_SUB_PROFILE_IDS = frozenset(
    sub_profile_id
    for sub_profile_ids in ENCLOSURE_SUB_VIEW_PROFILE_IDS.values()
    for sub_profile_id in sub_profile_ids
)


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


class MappingStore:
    """Persist slot-to-disk calibration in a small JSON file on a bind mount."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _slot_key(self, system_id: str | None, enclosure_id: str | None, slot: int) -> str:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        return f"{system_id or 'default_system'}:{physical_enclosure_id or 'default'}:{slot}"

    @staticmethod
    def _canonical_mapping(mapping: ManualMapping) -> ManualMapping:
        physical_enclosure_id = resolve_physical_mapping_scope(mapping.enclosure_id)
        return mapping.model_copy(update={"enclosure_id": physical_enclosure_id})

    @staticmethod
    def _drawer_alias_enclosure_ids(enclosure_id: str | None) -> tuple[str, ...]:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        if not physical_enclosure_id:
            return ()
        return tuple(
            f"{physical_enclosure_id}::{profile_id}"
            for profile_id in sorted(_DRAWER_SUB_PROFILE_IDS)
        )

    def _physical_scope_keys(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> tuple[str, ...]:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        keys = [self._slot_key(system_id, physical_enclosure_id, slot)]
        keys.extend(
            f"{system_id or 'default_system'}:{alias_id}:{slot}"
            for alias_id in self._drawer_alias_enclosure_ids(physical_enclosure_id)
        )
        return tuple(dict.fromkeys(keys))

    def _resolvable_keys(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> tuple[str, ...]:
        """Keys that can resolve for this bay: canonical, scoped enclosure-less
        sibling, then the two unscoped legacy aliases (see ``get_mapping``)."""
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        keys = [*self._physical_scope_keys(system_id, physical_enclosure_id, slot)]
        keys.append(self._slot_key(system_id, None, slot))
        keys.append(f"{physical_enclosure_id or 'default'}:{slot}")
        keys.extend(
            f"{alias_id}:{slot}"
            for alias_id in self._drawer_alias_enclosure_ids(physical_enclosure_id)
        )
        keys.append(f"default:{slot}")
        return tuple(dict.fromkeys(keys))

    def load_all(self) -> dict[str, ManualMapping]:
        if not self.file_path.exists():
            return {}

        # Tolerate a corrupt/truncated store the same way SlotDetailStore does:
        # a bad file must degrade to "no mappings", not 500 every snapshot build.
        try:
            with self.file_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return {}
            raw_mappings = payload.get("slot_mappings", {})
            if not isinstance(raw_mappings, dict):
                return {}
            loaded = {
                key: ManualMapping.model_validate(value)
                for key, value in raw_mappings.items()
            }
        except (OSError, json.JSONDecodeError, ValidationError):
            return {}
        return loaded

    def get_mapping(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        allow_legacy_fallback: bool = False,
        loaded_entries: Mapping[str, ManualMapping] | None = None,
    ) -> ManualMapping | None:
        current = self.load_all() if loaded_entries is None else loaded_entries
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        self._assert_no_alias_conflicts(
            current,
            system_id,
            physical_enclosure_id,
            slot=slot,
        )
        keys = (
            self._resolvable_keys(system_id, physical_enclosure_id, slot)
            if allow_legacy_fallback
            else self._physical_scope_keys(system_id, physical_enclosure_id, slot)
        )
        for key in keys:
            candidate = current.get(key)
            if candidate is None:
                continue
            if not self._mapping_matches_system(key, candidate, system_id):
                continue
            return self._canonical_mapping(candidate)
        return None

    def has_legacy_only_mapping(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        loaded_entries: Mapping[str, ManualMapping] | None = None,
    ) -> bool:
        """Report whether this bay has a legacy row and no exact scoped row."""
        current = self.load_all() if loaded_entries is None else loaded_entries
        if self.get_mapping(system_id, enclosure_id, slot, loaded_entries=current) is not None:
            return False
        return (
            self.get_mapping(
                system_id,
                enclosure_id,
                slot,
                allow_legacy_fallback=True,
                loaded_entries=current,
            )
            is not None
        )

    def count_for_system(self, system_id: str | None) -> int:
        mappings = self.load_all()
        if not system_id:
            return len(mappings)
        return len(self._scope_entries(mappings, system_id, None))

    def list_mappings(
        self,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> list[ManualMapping]:
        mappings = self.load_all()
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        if system_id is not None:
            selected_scope = self._scope_entries(mappings, system_id, physical_enclosure_id)
            return [
                selected_scope[identity]
                for identity in sorted(selected_scope, key=lambda item: (item[0] or "", item[1]))
            ]

        effective_system_ids = {
            self._mapping_system_id(key, mapping)
            for key, mapping in mappings.items()
        }
        for effective_system_id in effective_system_ids:
            conflict_system_id = (
                None
                if effective_system_id in {None, "default_system"}
                else effective_system_id
            )
            self._assert_no_alias_conflicts(
                mappings,
                conflict_system_id,
                physical_enclosure_id,
            )

        selected: dict[
            tuple[str | None, str | None, int],
            tuple[tuple[int, str], ManualMapping],
        ] = {}
        for key, mapping in mappings.items():
            canonical = self._canonical_mapping(mapping)
            if physical_enclosure_id is not None and canonical.enclosure_id != physical_enclosure_id:
                continue
            effective_system_id = self._mapping_system_id(key, mapping)
            identity = (
                effective_system_id,
                canonical.enclosure_id,
                canonical.slot,
            )
            canonical_key = self._slot_key(
                mapping.system_id,
                canonical.enclosure_id,
                canonical.slot,
            )
            rank = (2 if key == canonical_key else 1, key)
            existing = selected.get(identity)
            if existing is None or rank > existing[0]:
                selected[identity] = (rank, canonical)
        return [
            selected[identity][1]
            for identity in sorted(
                selected,
                key=lambda item: (item[0] or "", item[1] or "", item[2]),
            )
        ]

    def save_mapping(
        self,
        mapping: ManualMapping,
        *,
        expected_revision: str | None = None,
    ) -> ManualMapping:
        with self._lock:
            current = self.load_all()
            mapping = self._canonical_mapping(mapping)
            self._assert_no_alias_conflicts(
                current,
                mapping.system_id,
                mapping.enclosure_id,
                slot=mapping.slot,
            )
            if expected_revision is not None:
                current_revision = self._save_revision_from_current(
                    current,
                    mapping.system_id,
                    mapping.enclosure_id,
                    mapping.slot,
                )
                if current_revision != expected_revision:
                    raise MappingRevisionConflict(current_revision)
            saved = mapping.model_copy(
                update={"updated_at": datetime.now(timezone.utc)}
            )
            for stale_key in self._resolvable_keys(
                mapping.system_id, mapping.enclosure_id, mapping.slot
            ):
                current.pop(stale_key, None)
            current[self._slot_key(mapping.system_id, mapping.enclosure_id, mapping.slot)] = saved
            self._write(current)
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
            current = self.load_all()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            self._assert_no_alias_conflicts(current, system_id, enclosure_id, slot=slot)
            if expected_revision is not None:
                current_revision = self._clear_revision_from_current(
                    current, system_id, enclosure_id, slot
                )
                if current_revision != expected_revision:
                    raise MappingRevisionConflict(current_revision)
            effective: ManualMapping | None = None
            resolvable_keys = self._resolvable_keys(system_id, enclosure_id, slot)
            for key in resolvable_keys:
                candidate = current.get(key)
                if candidate is None:
                    continue
                if not self._mapping_matches_system(key, candidate, system_id):
                    continue
                effective = self._canonical_mapping(candidate)
                break
            if effective is None:
                return False
            effective_identity = self._scope_identity(effective)
            keys_to_remove = [
                key
                for key, mapping in current.items()
                if self._mapping_matches_clear_system(key, mapping, system_id)
                and (
                    self._scope_identity(mapping) == effective_identity
                    or key in resolvable_keys
                )
            ]
            for key in keys_to_remove:
                current.pop(key, None)
            self._write(current)
        return True

    def replace_mappings(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> int:
        with self._lock:
            current = self.load_all()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            self._assert_no_alias_conflicts(current, system_id, enclosure_id)
            incoming_scope = self._normalize_incoming(
                system_id,
                enclosure_id,
                mappings,
            )
            keys_to_remove: list[str] = []
            for key, mapping in current.items():
                if not self._mapping_matches_system(key, mapping, system_id):
                    continue
                canonical = self._canonical_mapping(mapping)
                if enclosure_id and canonical.enclosure_id not in {None, enclosure_id}:
                    continue
                keys_to_remove.append(key)

            for key in keys_to_remove:
                current.pop(key, None)

            saved_count = 0
            for mapping in incoming_scope.values():
                saved = mapping.model_copy(
                    update={"updated_at": datetime.now(timezone.utc)}
                )
                current[self._slot_key(saved.system_id, saved.enclosure_id, saved.slot)] = saved
                saved_count += 1

            self._write(current)
        return saved_count

    @staticmethod
    def _mapping_system_id(key: str, mapping: ManualMapping) -> str | None:
        if mapping.system_id is not None:
            return mapping.system_id
        legacy_keys = {
            f"{mapping.enclosure_id or 'default'}:{mapping.slot}",
            f"default:{mapping.slot}",
        }
        if key in legacy_keys:
            return None
        return key.split(":", 1)[0] if ":" in key else None

    @classmethod
    def _mapping_matches_system(
        cls,
        key: str,
        mapping: ManualMapping,
        system_id: str | None,
    ) -> bool:
        if system_id is None:
            return True
        return cls._mapping_system_id(key, mapping) in {None, system_id}

    @classmethod
    def _mapping_matches_clear_system(
        cls,
        key: str,
        mapping: ManualMapping,
        system_id: str | None,
    ) -> bool:
        mapping_system_id = cls._mapping_system_id(key, mapping)
        if system_id is None:
            return mapping_system_id in {None, "default_system"}
        return mapping_system_id in {None, system_id}

    def _mapping_precedence(
        self,
        key: str,
        mapping: ManualMapping,
        system_id: str | None,
    ) -> int:
        mapping_system_id = self._mapping_system_id(key, mapping)
        if mapping_system_id is None:
            return 0
        if system_id is None or mapping_system_id != system_id:
            return -1
        if key == self._slot_key(system_id, mapping.enclosure_id, mapping.slot):
            return 2
        return 1

    @staticmethod
    def _scope_identity(mapping: ManualMapping) -> tuple[str | None, int]:
        return resolve_physical_mapping_scope(mapping.enclosure_id), mapping.slot

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

    def _assert_no_alias_conflicts(
        self,
        current: Mapping[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
        *,
        slot: int | None = None,
    ) -> None:
        physical_enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        grouped: dict[
            tuple[str | None, int],
            list[tuple[bool, dict[str, Any]]],
        ] = {}
        for key, mapping in current.items():
            mapping_system_id = self._mapping_system_id(key, mapping)
            if system_id is not None:
                if mapping_system_id not in {None, system_id}:
                    continue
            elif mapping_system_id not in {None, "default_system"}:
                continue
            canonical = self._canonical_mapping(mapping)
            if canonical.enclosure_id != physical_enclosure_id and physical_enclosure_id is not None:
                continue
            if slot is not None and canonical.slot != slot:
                continue
            if canonical.enclosure_id is None:
                continue
            canonical_key = self._slot_key(system_id, canonical.enclosure_id, canonical.slot)
            alias_keys = set(
                self._physical_scope_keys(system_id, canonical.enclosure_id, canonical.slot)
            ) - {canonical_key}
            is_alias = (
                mapping.enclosure_id != canonical.enclosure_id
                or key in alias_keys
            )
            grouped.setdefault(self._scope_identity(canonical), []).append(
                (is_alias, self._semantic_mapping(canonical))
            )
        for rows in grouped.values():
            if not any(is_alias for is_alias, _semantic in rows):
                continue
            semantic_rows = {
                self._digest(semantic)
                for _is_alias, semantic in rows
            }
            if len(semantic_rows) > 1:
                raise MappingScopeConflict()

    @staticmethod
    def _digest(payload: Any) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _scope_entries(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[tuple[str | None, int], ManualMapping]:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        self._assert_no_alias_conflicts(current, system_id, enclosure_id)
        selected: dict[tuple[str | None, int], tuple[tuple[int, str], ManualMapping]] = {}
        for key, mapping in current.items():
            if not self._mapping_matches_system(key, mapping, system_id):
                continue
            canonical = self._canonical_mapping(mapping)
            if enclosure_id is not None and canonical.enclosure_id != enclosure_id:
                continue
            identity = self._scope_identity(canonical)
            rank = (self._mapping_precedence(key, canonical, system_id), key)
            existing = selected.get(identity)
            if existing is None or rank > existing[0]:
                selected[identity] = (rank, canonical)
        return {identity: ranked[1] for identity, ranked in selected.items()}

    def _normalize_incoming(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> dict[tuple[str | None, int], ManualMapping]:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        normalized: dict[tuple[str | None, int], ManualMapping] = {}
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
            identity = self._scope_identity(scoped)
            if identity in normalized:
                raise ValueError(f"Duplicate mapping for enclosure {identity[0] or 'default'} slot {identity[1]}.")
            normalized[identity] = scoped
        return normalized

    @staticmethod
    def _identity_payload(identity: tuple[str | None, int]) -> dict[str, Any]:
        return {"enclosure_id": identity[0], "slot": identity[1]}

    def _mapping_value_payload(self, mapping: ManualMapping) -> dict[str, Any]:
        return {
            key: value
            for key, value in self._semantic_mapping(mapping).items()
            if key not in {"enclosure_id", "slot"}
        }

    def _preview_from_current(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> tuple[dict[str, Any], dict[tuple[str | None, int], ManualMapping]]:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        current_scope = self._scope_entries(current, system_id, enclosure_id)
        incoming_scope = self._normalize_incoming(system_id, enclosure_id, mappings)
        current_payload = [
            self._semantic_mapping(current_scope[identity])
            for identity in sorted(current_scope, key=lambda item: (item[0] or "", item[1]))
        ]
        stored_rows = []
        for key, mapping in current.items():
            if not self._mapping_matches_system(key, mapping, system_id):
                continue
            canonical = self._canonical_mapping(mapping)
            if enclosure_id is not None and canonical.enclosure_id != enclosure_id:
                continue
            stored_rows.append({
                "key": key,
                "stored_system_id": mapping.system_id,
                "stored_enclosure_id": mapping.enclosure_id,
                "mapping": self._semantic_mapping(canonical),
            })
        revision = self._digest({
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "mappings": current_payload,
            "stored_rows": sorted(stored_rows, key=lambda item: item["key"]),
        })
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
                additions.append({
                    **target,
                    "incoming": self._mapping_value_payload(incoming_mapping),
                })
            elif incoming_mapping is None:
                removals.append({
                    **target,
                    "current": self._mapping_value_payload(current_mapping),
                })
            elif self._semantic_mapping(current_mapping) == self._semantic_mapping(incoming_mapping):
                unchanged.append(target)
            else:
                current_values = self._mapping_value_payload(current_mapping)
                incoming_values = self._mapping_value_payload(incoming_mapping)
                updates.append({
                    **target,
                    "changes": {
                        field: {"from": current_values[field], "to": incoming_values[field]}
                        for field in sorted(current_values)
                        if current_values[field] != incoming_values[field]
                    },
                })
        incoming_payload = [
            self._semantic_mapping(incoming_scope[identity])
            for identity in sorted(incoming_scope, key=lambda item: (item[0] or "", item[1]))
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
            current = self.load_all()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            preview, _incoming_scope = self._preview_from_current(
                current,
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
            current = self.load_all()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            return self._scope_revision_from_current(current, system_id, enclosure_id)

    def clear_revision(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        with self._lock:
            current = self.load_all()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            return self._clear_revision_from_current(current, system_id, enclosure_id, slot)

    def save_revision(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        with self._lock:
            current = self.load_all()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            return self._save_revision_from_current(current, system_id, enclosure_id, slot)

    def save_revisions(
        self,
        system_id: str | None,
        targets: list[tuple[str | None, int]],
    ) -> dict[tuple[str | None, int], str]:
        with self._lock:
            current = self.load_all()
            canonical_targets = {
                target: (resolve_physical_mapping_scope(target[0]), target[1])
                for target in targets
            }
            scope_revisions = {
                canonical[0]: self._scope_revision_from_current(
                    current, system_id, canonical[0]
                )
                for canonical in set(canonical_targets.values())
            }
            return {
                target: self._save_revision_from_current(
                    current,
                    system_id,
                    canonical[0],
                    canonical[1],
                    scope_revision=scope_revisions[canonical[0]],
                )
                for target, canonical in canonical_targets.items()
            }

    def clear_revisions(
        self,
        system_id: str | None,
        targets: list[tuple[str | None, int]],
    ) -> dict[tuple[str | None, int], str]:
        with self._lock:
            current = self.load_all()
            return {
                target: self._clear_revision_from_current(
                    current,
                    system_id,
                    resolve_physical_mapping_scope(target[0]),
                    target[1],
                )
                for target in targets
            }

    def _scope_revision_from_current(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        current_scope = self._scope_entries(current, system_id, enclosure_id)
        preview, _ = self._preview_from_current(
            current,
            system_id,
            enclosure_id,
            list(current_scope.values()),
        )
        return preview["revision"]

    def _resolvable_revision_from_current(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        entries = []
        for key in dict.fromkeys(self._resolvable_keys(system_id, enclosure_id, slot)):
            mapping = current.get(key)
            if mapping is None:
                continue
            entries.append({
                "key": key,
                "stored_system_id": mapping.system_id,
                "mapping": self._semantic_mapping(mapping),
            })
        return self._digest(
            {
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "slot": slot,
                "mappings": entries,
            }
        )

    def _save_revision_from_current(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        scope_revision: str | None = None,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        if scope_revision is None:
            scope_revision = self._scope_revision_from_current(
                current, system_id, enclosure_id
            )
        return self._digest(
            {
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "slot": slot,
                "scope_revision": scope_revision,
                "resolvable_revision": self._resolvable_revision_from_current(
                    current, system_id, enclosure_id, slot
                ),
            }
        )

    def _clear_revision_from_current(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        enclosure_id = resolve_physical_mapping_scope(enclosure_id)
        self._assert_no_alias_conflicts(current, system_id, enclosure_id, slot=slot)
        effective = None
        for key in self._resolvable_keys(system_id, enclosure_id, slot):
            candidate = current.get(key)
            if candidate is None:
                continue
            if not self._mapping_matches_system(key, candidate, system_id):
                continue
            effective = self._canonical_mapping(candidate)
            break
        effective_scope = effective.enclosure_id if effective is not None else enclosure_id
        payload = {
            "enclosure_id": enclosure_id,
            "slot": slot,
            "effective_scope": effective_scope,
            "effective_revision": self._scope_revision_from_current(
                current, system_id, effective_scope
            ),
            "resolvable_revision": self._resolvable_revision_from_current(
                current, system_id, enclosure_id, slot
            ),
        }
        return self._digest(payload)

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
            current = self.load_all()
            enclosure_id = resolve_physical_mapping_scope(enclosure_id)
            preview, incoming_scope = self._preview_from_current(
                current,
                system_id,
                enclosure_id,
                mappings,
            )
            if preview["revision"] != expected_revision:
                raise MappingRevisionConflict(preview["revision"])
            if preview["import_digest"] != import_digest:
                raise MappingImportDigestMismatch(preview["revision"], preview["import_digest"])

            keys_to_remove = [
                key
                for key, mapping in current.items()
                if self._mapping_matches_system(key, mapping, system_id)
                and (
                    enclosure_id is None
                    or resolve_physical_mapping_scope(mapping.enclosure_id) == enclosure_id
                )
            ]
            for key in keys_to_remove:
                current.pop(key, None)

            now = datetime.now(timezone.utc)
            for mapping in incoming_scope.values():
                saved = mapping.model_copy(update={"updated_at": now})
                current[self._slot_key(saved.system_id, saved.enclosure_id, saved.slot)] = saved
            self._write(current)

            final_preview, _ = self._preview_from_current(current, system_id, enclosure_id, list(incoming_scope.values()))
            return {
                "saved_count": len(incoming_scope),
                "revision": final_preview["revision"],
                "preview": preview,
            }

    def _write(self, mappings: dict[str, ManualMapping]) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "slot_mappings": {key: value.model_dump(mode="json") for key, value in mappings.items()},
        }
        temp_path = self.file_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        temp_path.replace(self.file_path)
