from __future__ import annotations

from typing import Any


SlotLayout = list[list[int | None]]


def normalize_slot_layout(value: Any) -> SlotLayout | None:
    """Normalize a profile slot matrix while preserving physical holes."""
    if value is None or value == "":
        return None
    if not isinstance(value, list):
        raise ValueError("slot_layout must be a list of rows.")

    normalized_rows: SlotLayout = []
    seen_slots: set[int] = set()
    for row in value:
        if not isinstance(row, list):
            raise ValueError("slot_layout rows must be lists.")
        normalized_row: list[int | None] = []
        for raw_slot in row:
            if raw_slot is None:
                normalized_row.append(None)
                continue
            if type(raw_slot) is not int:
                raise ValueError("slot_layout values must be non-negative integers or null.")
            slot_number = raw_slot
            if slot_number < 0:
                raise ValueError("slot_layout values must be non-negative integers or null.")
            if slot_number in seen_slots:
                raise ValueError("slot_layout slot numbers must be unique.")
            seen_slots.add(slot_number)
            normalized_row.append(slot_number)
        normalized_rows.append(normalized_row)
    return normalized_rows


def visible_slot_count(layout: SlotLayout | None) -> int:
    return sum(slot is not None for row in (layout or []) for slot in row)


def validate_slot_layout(
    layout: SlotLayout | None,
    *,
    rows: int,
    columns: int,
    slot_count: int | None = None,
) -> int | None:
    """Validate matrix geometry and return its authoritative physical count."""
    if layout is None:
        return slot_count
    if rows < 1 or columns < 1:
        raise ValueError("slot_layout requires positive rows and columns.")
    if len(layout) != rows:
        raise ValueError("slot_layout row count must match rows.")
    if any(len(row) != columns for row in layout):
        raise ValueError("slot_layout rows must have exactly columns entries.")

    count = visible_slot_count(layout)
    if slot_count is None:
        return count
    if any(slot is not None and slot >= slot_count for row in layout for slot in row):
        raise ValueError("slot_layout values must be less than slot_count.")
    if count != slot_count:
        raise ValueError("slot_layout must contain exactly slot_count visible slots.")
    return slot_count
