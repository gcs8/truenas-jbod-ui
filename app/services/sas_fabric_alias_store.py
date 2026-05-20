from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.models.domain import SasFabricAlias


class SasFabricAliasStore:
    """Persist operator-friendly names for SAS Fabric graph objects."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _key(self, system_id: str | None, enclosure_id: str | None, object_id: str) -> str:
        return f"{system_id or 'default_system'}:{enclosure_id or 'system'}:{object_id}"

    def load_all(self) -> dict[str, SasFabricAlias]:
        if not self.file_path.exists():
            return {}

        with self.file_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        loaded: dict[str, SasFabricAlias] = {}
        for key, value in payload.get("sas_fabric_aliases", {}).items():
            loaded[key] = SasFabricAlias.model_validate(value)
        return loaded

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

    def save_alias(self, alias: SasFabricAlias) -> SasFabricAlias:
        with self._lock:
            current = self.load_all()
            saved = alias.model_copy(update={"updated_at": datetime.now(timezone.utc)})
            current[self._key(saved.system_id, saved.enclosure_id, saved.object_id)] = saved
            self._write(current)
        return saved

    def clear_alias(self, system_id: str | None, enclosure_id: str | None, object_id: str) -> bool:
        with self._lock:
            current = self.load_all()
            removed = current.pop(self._key(system_id, enclosure_id, object_id), None)
            if removed is None:
                removed = current.pop(self._key(system_id, None, object_id), None)
            if removed is None:
                return False
            self._write(current)
        return True

    def _write(self, aliases: dict[str, SasFabricAlias]) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sas_fabric_aliases": {key: value.model_dump(mode="json") for key, value in aliases.items()},
        }
        temp_path = self.file_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        temp_path.replace(self.file_path)
