"""Test-only writers for mapping-store fixtures."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.models.domain import ManualMapping
from app.services.mapping_store import MappingStore


def write_v1_mappings(store: MappingStore, mappings: dict[str, ManualMapping]) -> None:
    """Write a version-1 mapping file; production writes only version 2."""
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "slot_mappings": {key: value.model_dump(mode="json") for key, value in mappings.items()},
    }
    store.file_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
