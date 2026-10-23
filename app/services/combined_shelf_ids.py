"""Saved state keyed by a combined SES shelf id such as ``A+B`` (#1006).

Before #1000, same-model shelves with different logical ids could be joined
into one default view whose id was ``"+".join(ids)``. Bay assignments, history
scopes, the saved main-page order and storage-view bindings were stored under
that id. Those shelves are now separate views, so the old keys no longer match
the views the app shows by default.

Nothing here moves saved data. It finds what is stored under a combined id
that is not offered now and, only when it is unambiguous, says where each
saved bay lands on the separate shelves. Unambiguous means every shelf in the
id is reported now with a known bay count, the counts are taken in the order
the combined view stacked them, and the bay falls inside exactly one shelf.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

COMBINED_ID_SEPARATOR = "+"
# Bounds for operator-facing text: a warning names at most this many scopes
# and bays, then counts the rest.
MAX_REPORTED_SCOPES = 4
MAX_REPORTED_BAYS = 8
SPLIT_SINCE = "from before same-model shelves were shown separately (#1000)"


def combined_enclosure_parts(enclosure_id: object) -> tuple[str, ...] | None:
    """The shelf ids a combined id joined, in stacking order, or None.

    Only the exact shape the parser produced counts: two or more distinct,
    non-empty ids without surrounding spaces or a ``:`` drawer/profile suffix.
    """

    if not isinstance(enclosure_id, str) or COMBINED_ID_SEPARATOR not in enclosure_id:
        return None
    parts = tuple(enclosure_id.split(COMBINED_ID_SEPARATOR))
    if len(parts) < 2 or len(set(parts)) != len(parts):
        return None
    if any(not part or part != part.strip() or ":" in part for part in parts):
        return None
    return parts


def locate_member_slot(slot: int, members: Sequence[tuple[str, int]]) -> tuple[str, int] | None:
    """Map a combined-view bay index to (shelf id, bay index on that shelf).

    The combined view drew each shelf's bays after the previous shelf's, in
    the id's order, so the offsets are the running sum of bay counts. Any
    unknown or empty count makes every offset after it unknown, so the answer
    is None rather than a guess.
    """

    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        return None
    offset = 0
    for member_id, count in members:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return None
        if slot < offset + count:
            return member_id, slot - offset
        offset += count
    return None


def _bay(slot: int) -> str:
    return f"bay {slot:02d}"


def _more(total: int, shown: int) -> str:
    return f"; and {total - shown} more" if total > shown else ""


def combined_mapping_warnings(
    scopes: Mapping[str, Sequence[int]],
    *,
    offered_ids: Iterable[str],
    bay_counts: Mapping[str, int | None],
    has_assignment: Mapping[tuple[str, int], bool],
    reopenable_ids: Iterable[str] = (),
) -> list[str]:
    """One warning per combined mapping scope that no offered view uses.

    ``scopes`` maps a combined id to its saved bay indexes. ``bay_counts``
    holds the bay count of every shelf offered now. ``has_assignment`` marks
    (shelf id, bay) pairs that already hold a saved assignment, so the text
    can flag its presence without comparing contents. ``reopenable_ids`` are
    combined ids the platform still opens when selected explicitly (CORE).
    """

    offered = set(offered_ids)
    reopenable = set(reopenable_ids)
    warnings: list[str] = []
    orphaned = sorted(
        (enclosure_id, sorted(set(slots)))
        for enclosure_id, slots in scopes.items()
        if enclosure_id not in offered and slots and combined_enclosure_parts(enclosure_id) is not None
    )
    for enclosure_id, slots in orphaned[:MAX_REPORTED_SCOPES]:
        parts = combined_enclosure_parts(enclosure_id) or ()
        count = len(slots)
        noun, verb = ("assignment is", "it is") if count == 1 else ("assignments are", "they are")
        lead = (
            f"{count} saved bay {noun} filed under the combined enclosure {enclosure_id} "
            f"{SPLIT_SINCE}, so {verb} not applied to the separate shelf views. Nothing was moved."
        )
        if enclosure_id in reopenable:
            lead += " That combined view still opens when its id is selected."
        members = [(part, bay_counts.get(part)) for part in parts]
        placeable = all(isinstance(bay_count, int) and bay_count > 0 for _part, bay_count in members)
        placements: list[str] = []
        unplaced = 0
        if placeable:
            for slot in slots:
                located = locate_member_slot(slot, members)  # type: ignore[arg-type]
                if located is None:
                    unplaced += 1
                    continue
                member_id, local_slot = located
                text = f"{_bay(slot)} -> {member_id} {_bay(local_slot)}"
                if has_assignment.get((member_id, local_slot)):
                    text += " (that bay already has a saved assignment)"
                placements.append(text)
        if not placeable:
            missing = [part for part, bay_count in members if not (isinstance(bay_count, int) and bay_count > 0)]
            warnings.append(
                f"{lead} Not every shelf it joined is reported now ({', '.join(missing)}), so where "
                "each bay belongs is not known. Check each bay before saving its assignment again."
            )
            continue
        detail = "; ".join(placements[:MAX_REPORTED_BAYS]) + _more(len(placements), MAX_REPORTED_BAYS)
        tail = (
            f" {unplaced} saved {'bay lies' if unplaced == 1 else 'bays lie'} past the last shelf "
            "and cannot be placed."
            if unplaced
            else ""
        )
        warnings.append(
            f"{lead} Open each shelf and save the assignment again where it belongs: {detail}.{tail}"
        )
    if len(orphaned) > MAX_REPORTED_SCOPES:
        warnings.append(
            f"{len(orphaned) - MAX_REPORTED_SCOPES} more combined enclosures hold saved bay assignments "
            "that are not applied."
        )
    return warnings


def combined_reference_warnings(
    *,
    offered_ids: Iterable[str],
    view_order: Iterable[str],
    storage_view_bindings: Iterable[tuple[str, Iterable[str]]],
) -> list[str]:
    """Name unused combined references only when at least one member is offered."""

    offered = set(offered_ids)
    warnings: list[str] = []
    order_ids = sorted({
        entry.removeprefix("enclosure:")
        for entry in view_order
        if isinstance(entry, str)
        and entry.startswith("enclosure:")
        and entry.removeprefix("enclosure:") not in offered
        and (parts := combined_enclosure_parts(entry.removeprefix("enclosure:"))) is not None
        and any(part in offered for part in parts)
    })
    if order_ids:
        shown = order_ids[:MAX_REPORTED_SCOPES]
        warnings.append(
            f"The saved main-page order names the combined enclosure {', '.join(shown)}"
            f"{_more(len(order_ids), len(shown))} {SPLIT_SINCE}. That entry is skipped; save the "
            "order again in System Setup to place the separate shelves."
        )
    for view_id, enclosure_ids in storage_view_bindings:
        stale = sorted({
            value
            for value in enclosure_ids
            if value not in offered and (parts := combined_enclosure_parts(value)) is not None
            and any(part in offered for part in parts)
        })
        if stale:
            shown = stale[:MAX_REPORTED_SCOPES]
            warnings.append(
                f"Storage view {view_id} is bound to the combined enclosure {', '.join(shown)}"
                f"{_more(len(stale), len(shown))} {SPLIT_SINCE}. Bind it to one of the separate "
                "shelves in System Setup."
            )
    return warnings


def combined_history_scope_warnings(scopes: Iterable[Mapping[str, object]]) -> list[dict[str, str]]:
    """Admin warnings for history rows still filed under a split combined id.

    History alone cannot see the live shelf list, so a combined scope counts
    as split only when the same system now records every shelf it joined as
    its own scope. A combined view that is still in use (a Front/Rear stack,
    or an explicit CORE view) never records its parts separately, so it is
    not reported. Nothing is moved.
    """

    by_system: dict[str, set[str]] = {}
    combined: list[tuple[str, str, str, int]] = []
    for scope in scopes:
        system_id = scope.get("system_id")
        enclosure_id = scope.get("enclosure_id")
        if not isinstance(system_id, str) or not isinstance(enclosure_id, str):
            continue
        by_system.setdefault(system_id, set()).add(enclosure_id)
        if combined_enclosure_parts(enclosure_id) is not None:
            label = scope.get("system_label")
            tracked = scope.get("tracked_slots")
            combined.append((
                system_id,
                label if isinstance(label, str) and label else system_id,
                enclosure_id,
                tracked if isinstance(tracked, int) and not isinstance(tracked, bool) else 0,
            ))
    split = [
        item
        for item in sorted(set(combined))
        if all(part in by_system.get(item[0], set()) for part in combined_enclosure_parts(item[2]) or ())
    ]
    warnings = [
        {
            "code": "combined_shelf_history_scope",
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "message": (
                f"History for {system_label} keeps {tracked} tracked bay{'s' if tracked != 1 else ''} under "
                f"the combined enclosure {enclosure_id} {SPLIT_SINCE}. New history is recorded under each "
                f"shelf ({', '.join(combined_enclosure_parts(enclosure_id) or ())}); the older rows stay "
                "under the combined id and are not shown with either shelf. Nothing was moved."
            ),
        }
        for system_id, system_label, enclosure_id, tracked in split[:MAX_REPORTED_SCOPES]
    ]
    if len(split) > MAX_REPORTED_SCOPES:
        warnings.append({
            "code": "combined_shelf_history_scope",
            "message": (
                f"{len(split) - MAX_REPORTED_SCOPES} more combined enclosures keep history under "
                "their old combined id."
            ),
        })
    return warnings
