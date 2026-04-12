from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.models.domain import ManualMapping


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

        with self.file_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        loaded: dict[str, ManualMapping] = {}
        for key, value in payload.get("slot_mappings", {}).items():
            loaded[key] = ManualMapping.model_validate(value)
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

        count = 0
        for key, mapping in mappings.items():
            if mapping.system_id == system_id or key.startswith(f"{system_id}:"):
                count += 1
        return count

    def list_mappings(
        self,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> list[ManualMapping]:
        mappings = self.load_all()
        selected: list[ManualMapping] = []
        for key, mapping in mappings.items():
            mapping_system_id = mapping.system_id or key.split(":", 1)[0]
            if system_id and mapping_system_id != system_id:
                continue
            if enclosure_id and mapping.enclosure_id not in {None, enclosure_id}:
                continue
            selected.append(mapping)
        return sorted(selected, key=lambda item: item.slot)

    def save_mapping(self, mapping: ManualMapping) -> ManualMapping:
        with self._lock:
            current = self.load_all()
            saved = mapping.model_copy(
                update={"updated_at": datetime.now(timezone.utc)}
            )
            current[self._slot_key(mapping.system_id, mapping.enclosure_id, mapping.slot)] = saved
            self._write(current)
        return saved

    def clear_mapping(self, system_id: str | None, enclosure_id: str | None, slot: int) -> bool:
        with self._lock:
            current = self.load_all()
            removed = current.pop(self._slot_key(system_id, enclosure_id, slot), None)
            if removed is None:
                removed = current.pop(self._slot_key(system_id, None, slot), None)
            if removed is None:
                removed = current.pop(f"{enclosure_id or 'default'}:{slot}", None)
            if removed is None:
                removed = current.pop(f"default:{slot}", None)
            if removed is None:
                return False
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
                mapping_system_id = mapping.system_id or key.split(":", 1)[0]
                if system_id and mapping_system_id != system_id:
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
