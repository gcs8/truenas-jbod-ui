from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.domain import ManualMapping


class MappingRevisionConflict(RuntimeError):
    def __init__(self, current_revision: str) -> None:
        super().__init__("Mapping scope revision changed before this write.")
        self.current_revision = current_revision


class MappingImportDigestMismatch(RuntimeError):
    def __init__(self, current_revision: str, current_import_digest: str) -> None:
        super().__init__("Mapping import digest does not match the confirmed preview.")
        self.current_revision = current_revision
        self.current_import_digest = current_import_digest


class MappingStore:
    """Persist slot-to-disk calibration in a small JSON file on a bind mount."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _slot_key(self, system_id: str | None, enclosure_id: str | None, slot: int) -> str:
        return f"{system_id or 'default_system'}:{enclosure_id or 'default'}:{slot}"

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

    def get_mapping(self, system_id: str | None, enclosure_id: str | None, slot: int) -> ManualMapping | None:
        current = self.load_all()
        return (
            current.get(self._slot_key(system_id, enclosure_id, slot))
            or current.get(self._slot_key(system_id, None, slot))
            or current.get(f"{enclosure_id or 'default'}:{slot}")
            or current.get(f"default:{slot}")
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
        if system_id is not None:
            selected_scope = self._scope_entries(mappings, system_id, enclosure_id)
            return [
                selected_scope[identity]
                for identity in sorted(selected_scope, key=lambda item: (item[0] or "", item[1]))
            ]

        selected: list[ManualMapping] = []
        for key, mapping in mappings.items():
            if not self._mapping_matches_system(key, mapping, system_id):
                continue
            if enclosure_id is not None and mapping.enclosure_id != enclosure_id:
                continue
            selected.append(mapping)
        return sorted(selected, key=lambda item: item.slot)

    def save_mapping(
        self,
        mapping: ManualMapping,
        *,
        expected_revision: str | None = None,
    ) -> ManualMapping:
        with self._lock:
            current = self.load_all()
            if expected_revision is not None:
                current_revision = self._scope_revision_from_current(
                    current, mapping.system_id, mapping.enclosure_id
                )
                if current_revision != expected_revision:
                    raise MappingRevisionConflict(current_revision)
            saved = mapping.model_copy(
                update={"updated_at": datetime.now(timezone.utc)}
            )
            current.pop(f"{mapping.enclosure_id or 'default'}:{mapping.slot}", None)
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
            if expected_revision is not None:
                current_revision = self._clear_revision_from_current(
                    current, system_id, enclosure_id, slot
                )
                if current_revision != expected_revision:
                    raise MappingRevisionConflict(current_revision)
            effective = None
            for key in (
                self._slot_key(system_id, enclosure_id, slot),
                self._slot_key(system_id, None, slot),
                f"{enclosure_id or 'default'}:{slot}",
                f"default:{slot}",
            ):
                candidate = current.get(key)
                if candidate is None:
                    continue
                if not self._mapping_matches_system(key, candidate, system_id):
                    continue
                effective = candidate
                break
            if effective is None:
                return False
            effective_identity = self._scope_identity(effective)
            keys_to_remove = [
                key
                for key, mapping in current.items()
                if self._mapping_matches_clear_system(key, mapping, system_id)
                and self._scope_identity(mapping) == effective_identity
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
            keys_to_remove: list[str] = []
            for key, mapping in current.items():
                if not self._mapping_matches_system(key, mapping, system_id):
                    continue
                if enclosure_id and mapping.enclosure_id not in {None, enclosure_id}:
                    continue
                keys_to_remove.append(key)

            for key in keys_to_remove:
                current.pop(key, None)

            saved_count = 0
            for mapping in mappings:
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
        return mapping.enclosure_id, mapping.slot

    @staticmethod
    def _semantic_mapping(mapping: ManualMapping) -> dict[str, Any]:
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
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _scope_entries(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[tuple[str | None, int], ManualMapping]:
        selected: dict[tuple[str | None, int], tuple[tuple[int, str], ManualMapping]] = {}
        for key, mapping in current.items():
            if not self._mapping_matches_system(key, mapping, system_id):
                continue
            if enclosure_id is not None and mapping.enclosure_id != enclosure_id:
                continue
            identity = self._scope_identity(mapping)
            rank = (self._mapping_precedence(key, mapping, system_id), key)
            existing = selected.get(identity)
            if existing is None or rank > existing[0]:
                selected[identity] = (rank, mapping)
        return {identity: ranked[1] for identity, ranked in selected.items()}

    def _normalize_incoming(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        mappings: list[ManualMapping],
    ) -> dict[tuple[str | None, int], ManualMapping]:
        normalized: dict[tuple[str | None, int], ManualMapping] = {}
        for mapping in mappings:
            scoped = mapping.model_copy(
                update={
                    "system_id": system_id,
                    "enclosure_id": enclosure_id if enclosure_id is not None else mapping.enclosure_id,
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
        current_scope = self._scope_entries(current, system_id, enclosure_id)
        incoming_scope = self._normalize_incoming(system_id, enclosure_id, mappings)
        current_payload = [
            self._semantic_mapping(current_scope[identity])
            for identity in sorted(current_scope, key=lambda item: (item[0] or "", item[1]))
        ]
        revision = self._digest(
            {
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "mappings": current_payload,
            }
        )
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
            return self._scope_revision_from_current(current, system_id, enclosure_id)

    def clear_revision(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        with self._lock:
            current = self.load_all()
            return self._clear_revision_from_current(current, system_id, enclosure_id, slot)

    def clear_revisions(
        self,
        system_id: str | None,
        targets: list[tuple[str | None, int]],
    ) -> dict[tuple[str | None, int], str]:
        with self._lock:
            current = self.load_all()
            return {
                target: self._clear_revision_from_current(
                    current, system_id, target[0], target[1]
                )
                for target in targets
            }

    def _scope_revision_from_current(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
    ) -> str:
        current_scope = self._scope_entries(current, system_id, enclosure_id)
        preview, _ = self._preview_from_current(
            current,
            system_id,
            enclosure_id,
            list(current_scope.values()),
        )
        return preview["revision"]

    def _clear_revision_from_current(
        self,
        current: dict[str, ManualMapping],
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
    ) -> str:
        effective = None
        for key in (
            self._slot_key(system_id, enclosure_id, slot),
            self._slot_key(system_id, None, slot),
            f"{enclosure_id or 'default'}:{slot}",
            f"default:{slot}",
        ):
            candidate = current.get(key)
            if candidate is None:
                continue
            if not self._mapping_matches_system(key, candidate, system_id):
                continue
            effective = candidate
            break
        effective_scope = effective.enclosure_id if effective is not None else enclosure_id
        payload = {
            "enclosure_id": enclosure_id,
            "slot": slot,
            "effective_scope": effective_scope,
            "effective_revision": self._scope_revision_from_current(
                current, system_id, effective_scope
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
                and (enclosure_id is None or mapping.enclosure_id == enclosure_id)
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
