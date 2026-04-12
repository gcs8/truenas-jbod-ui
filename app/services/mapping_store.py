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

    def _slot_key(self, enclosure_id: str | None, slot: int) -> str:
        return f"{enclosure_id or 'default'}:{slot}"

    def load_all(self) -> dict[str, ManualMapping]:
        if not self.file_path.exists():
            return {}

        with self.file_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        loaded: dict[str, ManualMapping] = {}
        for key, value in payload.get("slot_mappings", {}).items():
            loaded[key] = ManualMapping.model_validate(value)
        return loaded

    def get_mapping(self, enclosure_id: str | None, slot: int) -> ManualMapping | None:
        return self.load_all().get(self._slot_key(enclosure_id, slot))

    def save_mapping(self, mapping: ManualMapping) -> ManualMapping:
        with self._lock:
            current = self.load_all()
            saved = mapping.model_copy(
                update={"updated_at": datetime.now(timezone.utc)}
            )
            current[self._slot_key(mapping.enclosure_id, mapping.slot)] = saved
            self._write(current)
        return saved

    def clear_mapping(self, enclosure_id: str | None, slot: int) -> bool:
        with self._lock:
            current = self.load_all()
            removed = current.pop(self._slot_key(enclosure_id, slot), None)
            if removed is None:
                return False
            self._write(current)
        return True

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
