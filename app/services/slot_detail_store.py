from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.models.domain import utcnow


class SlotDetailCacheEntry(BaseModel):
    system_id: str | None = None
    enclosure_id: str | None = None
    slot: int
    identifiers: list[str] = Field(default_factory=list)
    slot_fields: dict[str, Any] = Field(default_factory=dict)
    smart_fields: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class SlotDetailStore:
    """Persist stable slot and SMART detail fields in a small local JSON file."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _slot_key(self, system_id: str | None, enclosure_id: str | None, slot: int) -> str:
        return f"{system_id or 'default_system'}:{enclosure_id or 'default'}:{slot}"

    def load_all(self) -> dict[str, SlotDetailCacheEntry]:
        if not self.file_path.exists():
            return {}

        try:
            with self.file_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

        loaded: dict[str, SlotDetailCacheEntry] = {}
        for key, value in payload.get("slot_details", {}).items():
            loaded[key] = SlotDetailCacheEntry.model_validate(value)
        return loaded

    def get_entry(self, system_id: str | None, enclosure_id: str | None, slot: int) -> SlotDetailCacheEntry | None:
        current = self.load_all()
        return current.get(self._slot_key(system_id, enclosure_id, slot))

    def save_entries(self, entries: list[SlotDetailCacheEntry]) -> None:
        if not entries:
            return

        with self._lock:
            current = self.load_all()
            for entry in entries:
                current[self._slot_key(entry.system_id, entry.enclosure_id, entry.slot)] = entry
            self._write(current)

    def _write(self, entries: dict[str, SlotDetailCacheEntry]) -> None:
        payload = {
            "version": 1,
            "updated_at": utcnow().isoformat(),
            "slot_details": {key: value.model_dump(mode="json") for key, value in entries.items()},
        }
        temp_path = self.file_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        temp_path.replace(self.file_path)
