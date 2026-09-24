"""Aggregate disk-retention accounting for inventory snapshots.

The system-scoped virtual inventory stops claiming physical bay locations when
no enclosure identity is trustworthy.  That is the correct fail-closed
behaviour, but it also means a release acceptance check cannot compare bay
counts between two snapshots to prove that no source disk was dropped: the
rendered shape legitimately changes.

This module provides the one identity normalization used by both the rendered
snapshot and the acceptance tests, plus the bounded aggregate totals that make
retention provable without exporting a single disk identifier:

``source_disk_count``
    How many disk records the platform source returned.
``rendered_unique_disk_count``
    How many distinct logical disks the rendered slots represent.  Several
    physical or multipath views of one disk collapse into one.
``duplicate_disk_view_count``
    How many rendered disk views beyond the first belong to a logical disk that
    is already represented.
``unplaced_disk_count``
    How many source disks are represented nowhere in the rendered inventory.
    A healthy snapshot -- physical or virtual -- reports zero.

Only integers leave this module.  Identity values are used to group records and
are never returned, so an acceptance check can consume the totals from a public
snapshot without handling serials, WWNs or GPT identifiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.services.parsers import (
    normalize_device_name,
    normalize_gptid,
    normalize_text,
)

__all__ = [
    "DiskRetentionAccounting",
    "build_disk_retention_accounting",
    "disk_record_identity_tokens",
    "logical_disk_identity_tokens",
    "slot_identity_tokens",
]


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = normalize_text(value)
    if normalized is None:
        return None
    return normalized.lower()


def logical_disk_identity_tokens(
    *,
    serial: Any = None,
    logical_unit_id: Any = None,
    gptid: Any = None,
    persistent_id_label: Any = None,
    device_names: Iterable[Any] = (),
) -> frozenset[str]:
    """Return the namespaced identity tokens of one logical disk.

    Tokens are namespaced so a device name can never be mistaken for a serial.
    Two records belong to the same logical disk when they share any token: a
    multipath view and its member, or two enclosure views of one disk, agree on
    at least one identifier even when the rest differ.
    """
    tokens: set[str] = set()
    serial_value = _clean(serial)
    if serial_value:
        tokens.add(f"serial:{serial_value}")
    lun_value = _clean(logical_unit_id)
    if lun_value:
        tokens.add(f"lun:{lun_value}")
    for raw_gptid in (gptid, persistent_id_label):
        gptid_value = _clean(raw_gptid)
        if not gptid_value:
            continue
        normalized_gptid = normalize_gptid(gptid_value)
        tokens.add(f"gptid:{(normalized_gptid or gptid_value).lower()}")
    for raw_device in device_names:
        device_value = _clean(raw_device)
        if not device_value:
            continue
        normalized_device = normalize_device_name(device_value)
        tokens.add(f"dev:{(normalized_device or device_value).lower()}")
    return frozenset(tokens)


def slot_identity_tokens(slot: Any) -> frozenset[str]:
    """Identity tokens of a rendered slot, or an empty set for an empty bay."""
    # Bay-scoped attributes (sas_address, slot number, enclosure id) are
    # deliberately not identity: they describe the bay, not its occupant.  An
    # occupied bay that carries none of the disk-scoped identifiers below
    # therefore produces no tokens and consumes no source disk.
    return logical_disk_identity_tokens(
        serial=getattr(slot, "serial", None),
        logical_unit_id=getattr(slot, "logical_unit_id", None),
        gptid=getattr(slot, "gptid", None),
        persistent_id_label=getattr(slot, "persistent_id_label", None),
        device_names=(
            getattr(slot, "device_name", None),
            *(getattr(slot, "smart_device_names", None) or ()),
            *(_multipath_names(slot)),
        ),
    )


def _multipath_names(slot: Any) -> tuple[Any, ...]:
    multipath = getattr(slot, "multipath", None)
    if multipath is None:
        return ()
    names: list[Any] = [
        getattr(multipath, "name", None),
        getattr(multipath, "device_name", None),
        getattr(multipath, "path_device_name", None),
        getattr(multipath, "alternate_path_device", None),
    ]
    for member in getattr(multipath, "members", None) or ():
        names.append(member if isinstance(member, str) else getattr(member, "device_name", None))
    return tuple(names)


def disk_record_identity_tokens(disk: Any) -> frozenset[str]:
    """Identity tokens of one source disk record."""
    return logical_disk_identity_tokens(
        serial=getattr(disk, "serial", None),
        logical_unit_id=getattr(disk, "lunid", None),
        gptid=getattr(disk, "identifier", None),
        device_names=(
            getattr(disk, "device_name", None),
            getattr(disk, "path_device_name", None),
            getattr(disk, "multipath_name", None),
            getattr(disk, "multipath_member", None),
            *(getattr(disk, "smart_devices", None) or ()),
        ),
    )


@dataclass(frozen=True, slots=True)
class DiskRetentionAccounting:
    """Bounded totals proving every source disk reached the rendered view."""

    source_disk_count: int = 0
    rendered_unique_disk_count: int = 0
    duplicate_disk_view_count: int = 0
    unplaced_disk_count: int = 0


class _Classes:
    """Minimal union-find over identity tokens."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def _find(self, token: str) -> str:
        parent = self._parent.setdefault(token, token)
        while parent != token:
            token = parent
            parent = self._parent.setdefault(token, token)
        return token

    def union(self, tokens: Iterable[str]) -> None:
        roots = {self._find(token) for token in tokens}
        if len(roots) <= 1:
            return
        primary = min(roots)
        for root in roots:
            self._parent[root] = primary

    def classify(self, tokens: Iterable[str]) -> str | None:
        roots = sorted({self._find(token) for token in tokens})
        if not roots:
            return None
        return roots[0]


def build_disk_retention_accounting(
    *,
    source_disks: Sequence[Any],
    slots: Sequence[Any],
) -> DiskRetentionAccounting:
    """Reconcile the rendered inventory against the platform's source disks.

    A source disk that carries no usable identifier is reported as unplaced: it
    cannot be proven to have reached the rendered view, and a release gate must
    fail closed rather than assume retention.
    """
    source_tokens = [disk_record_identity_tokens(disk) for disk in source_disks]
    rendered_tokens = [tokens for tokens in (slot_identity_tokens(slot) for slot in slots) if tokens]

    classes = _Classes()
    for tokens in (*source_tokens, *rendered_tokens):
        classes.union(tokens)

    rendered_classes: set[str] = set()
    rendered_view_count = 0
    for tokens in rendered_tokens:
        identity = classes.classify(tokens)
        if identity is None:
            continue
        rendered_view_count += 1
        rendered_classes.add(identity)

    unplaced = 0
    for tokens in source_tokens:
        identity = classes.classify(tokens)
        if identity is None or identity not in rendered_classes:
            unplaced += 1

    return DiskRetentionAccounting(
        source_disk_count=len(source_tokens),
        rendered_unique_disk_count=len(rendered_classes),
        duplicate_disk_view_count=rendered_view_count - len(rendered_classes),
        unplaced_disk_count=unplaced,
    )
