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

import re
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
    "scope_node_local_tokens",
    "slot_identity_tokens",
]


# A bare device-mapper minor name (dm-12) is handed out in creation order on
# each node, so two disks can carry the same one and it names neither (#921).
# Stable device-mapper names such as by-id/dm-uuid-mpath-... stay identity.
_BARE_DEVICE_MAPPER_NAME = re.compile(r"^(?:/dev/)?dm-\d+$", re.IGNORECASE)


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

    ``persistent_id_label`` is accepted for caller compatibility but ignored:
    labels such as "GPTID" describe an identifier type, not a disk identity.
    A bare device-mapper name such as ``dm-12`` is not identity either.
    """
    tokens: set[str] = set()
    serial_value = _clean(serial)
    if serial_value:
        tokens.add(f"serial:{serial_value}")
    lun_value = _clean(logical_unit_id)
    if lun_value:
        tokens.add(f"lun:{lun_value}")
    gptid_value = _clean(gptid)
    if gptid_value and not _BARE_DEVICE_MAPPER_NAME.match(gptid_value):
        normalized_gptid = normalize_gptid(gptid_value)
        tokens.add(f"gptid:{(normalized_gptid or gptid_value).lower()}")
    for raw_device in device_names:
        device_value = _clean(raw_device)
        if not device_value or _BARE_DEVICE_MAPPER_NAME.match(device_value):
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


def scope_node_local_tokens(tokens: frozenset[str], scope: str | None) -> frozenset[str]:
    """Namespace node-local identity tokens under ``scope`` (#917).

    On a QuantaStor HA pair each node names its own disks, so ``/dev/sda`` on
    one node and ``/dev/sda`` on the other can be different disks. Device names,
    and a GPT identifier that is only a device path, are qualified by the node;
    serials, LUN ids and real persistent identifiers stay global, so a shared
    disk seen from both nodes is still one disk.
    """
    if scope is None:
        return tokens
    device_values = {token[len("dev:"):] for token in tokens if token.startswith("dev:")}
    scoped: set[str] = set()
    for token in tokens:
        node_local = token.startswith("dev:") or (
            token.startswith("gptid:") and token[len("gptid:"):] in device_values
        )
        scoped.add(f"node[{scope}]/{token}" if node_local else token)
    return frozenset(scoped)


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

    def find(self, token: str) -> str:
        parent = self._parent.setdefault(token, token)
        while parent != token:
            token = parent
            parent = self._parent.setdefault(token, token)
        return token

    def union(self, tokens: Iterable[str]) -> None:
        roots = {self.find(token) for token in tokens}
        if len(roots) <= 1:
            return
        primary = min(roots)
        for root in roots:
            self._parent[root] = primary


# Serials and LUN ids name a disk wherever it is seen; device names and GPT
# identifiers only name a path to it.
_STABLE_PREFIXES = ("serial:", "lun:")


def _identity_classes(token_sets: Sequence[frozenset[str]]) -> list[str | None]:
    """The logical disk of each token set, or None when it has none (#921).

    Token sets that share any token are one disk, unless that would put two
    different stable identities in one class: two serials, or two LUN ids that
    no serial ties together. Such a group is split by its stable identities.
    A record without a serial or LUN id of its own then joins the one identity
    its names lead to; if they lead to several, it is ambiguous and is credited
    to none of them.
    """
    # A LUN id reported with two different serials (a RAID volume id carried
    # by every member disk, say) identifies none of them; it is kept only as
    # a shared name, like a device name.
    lun_serials: dict[str, set[str]] = {}
    for tokens in token_sets:
        serials = {token for token in tokens if token.startswith("serial:")}
        for token in tokens:
            if token.startswith("lun:"):
                lun_serials.setdefault(token, set()).update(serials)
    shared_luns = {lun for lun, serials in lun_serials.items() if len(serials) > 1}
    if shared_luns:
        token_sets = [
            frozenset(f"shared-{token}" if token in shared_luns else token for token in tokens)
            for tokens in token_sets
        ]

    stable = _Classes()
    components = _Classes()
    for tokens in token_sets:
        stable.union(token for token in tokens if token.startswith(_STABLE_PREFIXES))
        components.union(tokens)

    # A component conflicts when more than one of its stable classes carries
    # a serial, or more than one carries a LUN id.
    carriers: dict[tuple[str, str], set[str]] = {}
    for tokens in token_sets:
        for token in tokens:
            if token.startswith(_STABLE_PREFIXES):
                kind = token.split(":", 1)[0]
                carriers.setdefault((kind, components.find(token)), set()).add(stable.find(token))
    conflicting = {component for (_kind, component), roots in carriers.items() if len(roots) > 1}

    def stable_tokens(tokens: frozenset[str]) -> list[str]:
        return [token for token in tokens if token.startswith(_STABLE_PREFIXES)]

    # Inside a conflicting component a name never joins two stable classes.
    # It is attached to the stable classes of the records that carry it, and
    # records without a stable identity are grouped by their shared names.
    names = _Classes()
    attached: dict[str, set[str]] = {}
    for tokens in token_sets:
        if not tokens or components.find(next(iter(tokens))) not in conflicting:
            continue
        own = stable_tokens(tokens)
        if own:
            root = stable.find(own[0])
            for token in tokens:
                if not token.startswith(_STABLE_PREFIXES):
                    attached.setdefault(token, set()).add(root)
        else:
            names.union(tokens)
    attached_by_group: dict[str, set[str]] = {}
    for token, roots in attached.items():
        attached_by_group.setdefault(names.find(token), set()).update(roots)

    classes: list[str | None] = []
    for tokens in token_sets:
        if not tokens:
            classes.append(None)
            continue
        component = components.find(next(iter(tokens)))
        if component not in conflicting:
            classes.append(f"disk:{component}")
            continue
        own = stable_tokens(tokens)
        if own:
            classes.append(f"stable:{stable.find(own[0])}")
            continue
        group = names.find(next(iter(tokens)))
        roots = attached_by_group.get(group, set())
        if len(roots) == 1:
            classes.append(f"stable:{next(iter(roots))}")
        elif not roots:
            classes.append(f"names:{group}")
        else:
            classes.append(None)
    return classes


def build_disk_retention_accounting(
    *,
    source_disks: Sequence[Any],
    slots: Sequence[Any],
    source_scopes: Sequence[str | None] | None = None,
    slot_scopes: Sequence[str | None] | None = None,
) -> DiskRetentionAccounting:
    """Reconcile the rendered inventory against the platform's source disks.

    A source disk that carries no usable identifier is reported as unplaced: it
    cannot be proven to have reached the rendered view, and a release gate must
    fail closed rather than assume retention.

    ``source_scopes`` and ``slot_scopes``, when given, name the node each
    record or slot was read from; see scope_node_local_tokens.
    """
    source_scope_list = list(source_scopes) if source_scopes is not None else [None] * len(source_disks)
    slot_scope_list = list(slot_scopes) if slot_scopes is not None else [None] * len(slots)
    if len(source_scope_list) != len(source_disks) or len(slot_scope_list) != len(slots):
        raise ValueError("one scope is required per source disk and per slot")
    source_tokens = [
        scope_node_local_tokens(disk_record_identity_tokens(disk), scope)
        for disk, scope in zip(source_disks, source_scope_list)
    ]
    rendered_tokens = [
        tokens
        for tokens in (
            scope_node_local_tokens(slot_identity_tokens(slot), scope)
            for slot, scope in zip(slots, slot_scope_list)
        )
        if tokens
    ]

    identities = _identity_classes([*source_tokens, *rendered_tokens])
    source_identities = identities[: len(source_tokens)]
    rendered_identities = identities[len(source_tokens):]

    rendered_classes: set[str] = set()
    rendered_view_count = 0
    for identity in rendered_identities:
        if identity is None:
            continue
        rendered_view_count += 1
        rendered_classes.add(identity)

    unplaced = 0
    for identity in source_identities:
        if identity is None or identity not in rendered_classes:
            unplaced += 1

    return DiskRetentionAccounting(
        source_disk_count=len(source_tokens),
        rendered_unique_disk_count=len(rendered_classes),
        duplicate_disk_view_count=rendered_view_count - len(rendered_classes),
        unplaced_disk_count=unplaced,
    )
