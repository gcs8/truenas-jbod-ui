from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.models.domain import utcnow


class SlotDetailCacheEntry(BaseModel):
    system_id: str | None = None
    enclosure_id: str | None = None
    slot: int
    identifiers: list[str] = Field(default_factory=list)
    slot_fields: dict[str, Any] = Field(default_factory=dict)
    smart_fields: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


def _entry_facts(entry: SlotDetailCacheEntry) -> dict[str, Any]:
    return entry.model_dump(mode="json", exclude={"updated_at"})


class SlotDetailStore:
    """Persist stable slot and SMART detail fields in a small local JSON file."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _slot_key(self, system_id: str | None, enclosure_id: str | None, slot: int) -> str:
        return f"{system_id or 'default_system'}:{enclosure_id or 'default'}:{slot}"

    def entry_key(self, entry: SlotDetailCacheEntry) -> str:
        return self._slot_key(entry.system_id, entry.enclosure_id, entry.slot)

    @staticmethod
    def merge_entry(
        existing: SlotDetailCacheEntry | None,
        entry: SlotDetailCacheEntry,
    ) -> SlotDetailCacheEntry | None:
        """Return the row to store for `entry`, or None when nothing would change.

        A snapshot build knows a bay's slot fields but not its SMART fields, so
        an incoming row without SMART fields keeps the SMART fields already
        stored for the same disk (any shared identifier). Rows are compared
        without `updated_at`, so re-saving the same facts is not a change.
        """
        if existing is not None and not entry.smart_fields and existing.smart_fields:
            shared = {item.lower() for item in existing.identifiers} & {item.lower() for item in entry.identifiers}
            if shared:
                entry = entry.model_copy(update={"smart_fields": dict(existing.smart_fields)})
        if existing is not None and _entry_facts(existing) == _entry_facts(entry):
            return None
        return entry

    def changed_entries(
        self,
        entries: Iterable[SlotDetailCacheEntry],
        loaded_entries: Mapping[str, SlotDetailCacheEntry],
    ) -> list[SlotDetailCacheEntry]:
        """Return only the rows that differ from `loaded_entries`, merged as they would be saved."""
        changed: list[SlotDetailCacheEntry] = []
        for entry in entries:
            merged = self.merge_entry(loaded_entries.get(self.entry_key(entry)), entry)
            if merged is not None:
                changed.append(merged)
        return changed

    def load_all(self) -> dict[str, SlotDetailCacheEntry]:
        if not self.file_path.exists():
            return {}

        try:
            with self.file_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(payload, dict):
            return {}
        raw_entries = payload.get("slot_details", {})
        if not isinstance(raw_entries, dict):
            return {}

        loaded: dict[str, SlotDetailCacheEntry] = {}
        for key, value in raw_entries.items():
            try:
                loaded[key] = SlotDetailCacheEntry.model_validate(value)
            except (TypeError, ValidationError):
                continue
        return loaded

    def get_entry(
        self,
        system_id: str | None,
        enclosure_id: str | None,
        slot: int,
        *,
        loaded_entries: Mapping[str, SlotDetailCacheEntry] | None = None,
    ) -> SlotDetailCacheEntry | None:
        current = self.load_all() if loaded_entries is None else loaded_entries
        return current.get(self._slot_key(system_id, enclosure_id, slot))

    def save_entries(self, entries: Iterable[SlotDetailCacheEntry]) -> int:
        """Merge `entries` into the file and return how many rows changed.

        The file is re-read under the lock so concurrent savers for other
        systems are never overwritten, and it is only rewritten when at least
        one row actually changed.
        """
        entry_list = list(entries)
        if not entry_list:
            return 0

        with self._lock:
            current = self.load_all()
            changed = 0
            for entry in entry_list:
                key = self.entry_key(entry)
                merged = self.merge_entry(current.get(key), entry)
                if merged is None:
                    continue
                current[key] = merged
                changed += 1
            if changed:
                self._write(current)
            return changed

    def prune_unknown_systems(self, valid_system_ids: set[str]) -> int:
        with self._lock:
            try:
                current = self.load_all()
            except (AttributeError, TypeError, ValidationError):
                return 0
            retained = {
                key: entry
                for key, entry in current.items()
                if entry.system_id in valid_system_ids
            }
            removed = len(current) - len(retained)
            if removed:
                self._write(retained)
            return removed

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
