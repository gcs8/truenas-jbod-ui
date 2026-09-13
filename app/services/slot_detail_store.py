from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
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

    def save_entries(
        self, entries: list[SlotDetailCacheEntry], *,
        expected_entries: Mapping[str, SlotDetailCacheEntry] | None = None,
        commit_guard: Callable[[], AbstractContextManager[bool]] | None = None,
    ) -> None:
        if not entries:
            return
        if commit_guard is not None:
            with commit_guard() as valid:
                if not valid:
                    return

        with self._lock:
            current = self.load_all()
            merged = current.copy()
            for entry in entries:
                key = self._slot_key(entry.system_id, entry.enclosure_id, entry.slot)
                if expected_entries is not None:
                    expected = expected_entries.get(key)
                    actual = current.get(key)
                    # A batch may span remote awaits. Do not replace a slot that
                    # another writer changed or removed since that batch read it.
                    expected_json = None if expected is None else json.dumps(expected.model_dump(mode="json"), sort_keys=True)
                    actual_json = None if actual is None else json.dumps(actual.model_dump(mode="json"), sort_keys=True)
                    if expected_json != actual_json:
                        continue
                merged[key] = entry
            # Compare final full JSON payloads, including freshness and identity.
            # Model equality alone conflates JSON booleans, integers and floats.
            if all(
                key in current and (
                    entry is current[key]
                    or json.dumps(entry.model_dump(mode="json"), sort_keys=True)
                    == json.dumps(current[key].model_dump(mode="json"), sort_keys=True)
                )
                for key, entry in merged.items()
            ):
                return
            if commit_guard is None:
                self._write(merged)
            else:
                self._write(merged, commit_guard=commit_guard)

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

    def _write(
        self, entries: dict[str, SlotDetailCacheEntry], *,
        commit_guard: Callable[[], AbstractContextManager[bool]] | None = None,
    ) -> None:
        payload = {
            "version": 1,
            "updated_at": utcnow().isoformat(),
            "slot_details": {key: value.model_dump(mode="json") for key, value in entries.items()},
        }
        temp_path = self.file_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        # The store lock still owns the reload/merge/write. A caller's generation
        # guard serializes just publication with invalidation, not JSON I/O.
        with commit_guard() if commit_guard is not None else nullcontext(True) as valid:
            if valid:
                temp_path.replace(self.file_path)
                return
        temp_path.unlink()
