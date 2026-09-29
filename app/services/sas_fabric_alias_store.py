from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.models.domain import SasFabricAlias
from app.services.config_change_journal import record_config_change
from app.services.storage_writability import (
    StorageDirectoryUnwritable,
    is_unwritable_error,
)


class SasFabricAliasStorageUnwritable(StorageDirectoryUnwritable):
    """The alias file could not be written because its directory is read-only."""

    error_code = "data_directory_unwritable"


class SasFabricAliasStore:
    """Persist operator-friendly names for SAS Fabric graph objects."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _key(self, system_id: str | None, enclosure_id: str | None, object_id: str) -> str:
        return f"{system_id or 'default_system'}:{enclosure_id or 'system'}:{object_id}"

    def _load_authoritative(self) -> dict[str, SasFabricAlias]:
        try:
            handle = self.file_path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ValueError("Cannot read the authoritative SAS Fabric alias store; no changes were saved.") from exc
        try:
            with handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ValueError("Cannot read the authoritative SAS Fabric alias store; no changes were saved.") from exc
        try:
            if not isinstance(payload, dict) or not isinstance(payload.get("sas_fabric_aliases"), dict):
                raise ValueError("Invalid alias store shape")
            return {
                key: SasFabricAlias.model_validate(value)
                for key, value in payload["sas_fabric_aliases"].items()
            }
        except ValueError as exc:
            raise ValueError("Invalid authoritative SAS Fabric alias store; no changes were saved.") from exc

    def load_all(self) -> dict[str, SasFabricAlias]:
        """Tolerant display read. Mutations must use the strict reader under lock."""
        try:
            return self._load_authoritative()
        except ValueError:
            return {}

    def list_aliases(self, system_id: str | None = None, enclosure_id: str | None = None) -> list[SasFabricAlias]:
        aliases = self.load_all()
        selected: dict[str, SasFabricAlias] = {}
        sorted_aliases = sorted(aliases.items(), key=lambda item: item[1].enclosure_id is not None)
        for key, alias in sorted_aliases:
            alias_system_id = alias.system_id or key.split(":", 1)[0]
            if system_id and alias_system_id != system_id:
                continue
            if alias.enclosure_id not in {None, enclosure_id}:
                continue
            # System-scoped aliases are loaded first and enclosure-scoped aliases
            # override them for the same object in the selected physical view.
            selected[alias.object_id] = alias
        return sorted(selected.values(), key=lambda item: (item.object_kind or "", item.object_id))

    def _mutation_keys(
        self,
        current: dict[str, SasFabricAlias],
        system_id: str | None,
        enclosure_id: str | None,
        object_id: str,
        compatible_object_ids: Iterable[str],
        legacy_owners: dict[str, set[str]] | None,
    ) -> list[str]:
        compatible = set(compatible_object_ids)
        keys = []
        for candidate_id in sorted({object_id, *compatible}):
            key = self._key(system_id, enclosure_id, candidate_id)
            existing = current.get(key)
            if existing is None:
                continue
            if existing.source == "operator-canonical-v1":
                if candidate_id == object_id:
                    keys.append(key)
                continue
            # Even an exact key may be a legacy token shared by distinct
            # canonical objects. No provenance means no destructive guess.
            if candidate_id in compatible or (legacy_owners is not None and candidate_id in legacy_owners):
                if existing.source != "operator" or (legacy_owners or {}).get(candidate_id) != {object_id}:
                    raise ValueError("SAS Fabric alias ownership is ambiguous or unavailable; no changes were saved.")
            keys.append(key)
        return keys

    def save_alias(
        self,
        alias: SasFabricAlias,
        compatible_object_ids: Iterable[str] = (),
        *,
        legacy_owners: dict[str, set[str]] | None = None,
    ) -> SasFabricAlias:
        with self._lock:
            current = self._load_authoritative()
            keys = self._mutation_keys(
                current, alias.system_id, alias.enclosure_id, alias.object_id,
                compatible_object_ids, legacy_owners,
            )
            saved = alias.model_copy(update={"updated_at": datetime.now(timezone.utc)})
            for key in keys:
                current.pop(key)
            current[self._key(saved.system_id, saved.enclosure_id, saved.object_id)] = saved
            self._write(current)
        record_config_change("sas_alias.save", f"{saved.system_id or ''}:{saved.object_id}")
        return saved

    def clear_alias(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        object_id: str,
        compatible_object_ids: Iterable[str] = (),
        *,
        legacy_owners: dict[str, set[str]] | None = None,
    ) -> bool:
        with self._lock:
            current = self._load_authoritative()
            compatible_object_ids = tuple(compatible_object_ids)
            keys = self._mutation_keys(
                current, system_id, enclosure_id, object_id, compatible_object_ids, legacy_owners,
            )
            if not keys and enclosure_id is not None:
                keys = self._mutation_keys(
                    current, system_id, None, object_id, compatible_object_ids, legacy_owners,
                )
            if not keys:
                return False
            for key in keys:
                current.pop(key)
            self._write(current)
        record_config_change("sas_alias.clear", f"{system_id or ''}:{object_id}")
        return True

    def _write(self, aliases: dict[str, SasFabricAlias]) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sas_fabric_aliases": {key: value.model_dump(mode="json") for key, value in aliases.items()},
        }
        temp_path = self.file_path.with_suffix(".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            temp_path.replace(self.file_path)
        except OSError as exc:
            if is_unwritable_error(exc):
                raise SasFabricAliasStorageUnwritable(self.file_path.parent) from exc
            raise
