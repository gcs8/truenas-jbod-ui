"""CORE (FreeBSD ``mpr``) trace, parent, lane and counter projection for the SAS fabric.

This code was moved here from ``app.services.sas_fabric`` without changes. That
module imports every name the moved code defines, so callers and tests keep using it.

The projection also uses helpers and ancestry limits that stay in
``app.services.sas_fabric``. This module does not import that module. Instead
``app.services.sas_fabric`` passes itself to ``bind_facade`` when it is imported. The
wrappers below and the two limit reads look the name up on it at call time, so a
replacement made on ``app.services.sas_fabric`` still reaches the moved code.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence

from app.models.domain import (
    InventorySnapshot,
    SasFabricLink,
    SasFabricNode,
    SasFabricReportedLane,
    SasFabricReportedLanes,
    SasFabricTrace,
    SlotView,
)
from app.services.core_expander_counters import (
    CAPABILITY_DENIED as SMP_CAPABILITY_DENIED,
    COUNTER_FIELDS as SMP_COUNTER_FIELDS,
    HUNG_MESSAGE as SMP_HUNG_MESSAGE,
    NO_ANSWER_MESSAGE as SMP_NO_ANSWER_MESSAGE,
    NO_ROUTE_MESSAGE as SMP_NO_ROUTE_MESSAGE,
    NOT_READ_MESSAGE as SMP_NOT_READ_MESSAGE,
    PERMISSION_DENIED_MESSAGE as SMP_PERMISSION_DENIED_MESSAGE,
    READ_BUDGET_MESSAGE as SMP_READ_BUDGET_MESSAGE,
    READ_BUDGET_REASON as SMP_READ_BUDGET_REASON,
    REJECTED_OUTPUT_MESSAGE as SMP_REJECTED_OUTPUT_MESSAGE,
    STOP_REASON_LABELS as SMP_STOP_REASON_LABELS,
    KeptCounterRead,
    SMP_MAX_CALLS_PER_REFRESH,
    SMP_MAX_TARGETS_PER_REFRESH,
    SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX,
    counter_summary as smp_counter_summary,
    discover_smp_targets,
    parse_chunk_output,
)
from app.services.parsers import normalize_text


_facade: ModuleType | None = None


def bind_facade(facade: ModuleType) -> None:
    """Record the module whose helpers and limits the moved code uses."""
    global _facade
    _facade = facade


def add_node(*args: Any, **kwargs: Any) -> Any:
    return _facade.add_node(*args, **kwargs)


def add_link(*args: Any, **kwargs: Any) -> Any:
    return _facade.add_link(*args, **kwargs)


def _mpr_controller_name_for_unit(*args: Any, **kwargs: Any) -> Any:
    return _facade._mpr_controller_name_for_unit(*args, **kwargs)


def _mpr_controller_ids_by_unit(*args: Any, **kwargs: Any) -> Any:
    return _facade._mpr_controller_ids_by_unit(*args, **kwargs)


def slots_overlap(*args: Any, **kwargs: Any) -> Any:
    return _facade.slots_overlap(*args, **kwargs)


def _snapshot_slot_numbers(*args: Any, **kwargs: Any) -> Any:
    return _facade._snapshot_slot_numbers(*args, **kwargs)


def _snapshot_disk_slot_numbers(*args: Any, **kwargs: Any) -> Any:
    return _facade._snapshot_disk_slot_numbers(*args, **kwargs)


def _dedupe_strings(*args: Any, **kwargs: Any) -> Any:
    return _facade._dedupe_strings(*args, **kwargs)


def _dedupe_ints(*args: Any, **kwargs: Any) -> Any:
    return _facade._dedupe_ints(*args, **kwargs)


def _build_mpr_trace_index(
    *,
    unit_data: dict[int, dict[str, Any]],
    controllers: list[dict[str, Any]],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    controller_ids_by_unit = _mpr_controller_ids_by_unit(controllers)
    devices: dict[tuple[str, str], dict[str, Any] | None] = {}
    devices_by_location: dict[tuple[str, str, int], dict[str, Any] | None] = {}
    devices_by_handle_location: dict[tuple[str, str, int], dict[str, Any] | None] = {}
    devices_by_target: dict[tuple[str, int], dict[str, Any] | None] = {}
    enclosures: dict[tuple[str, str], str] = {}
    enclosure_keys_by_handle: dict[tuple[str, str], str] = {}
    expanders: dict[tuple[str, str], list[str]] = defaultdict(list)

    for unit, payload in sorted(unit_data.items()):
        controller_id = controller_ids_by_unit.get(unit, f"controller:{_mpr_controller_name_for_unit(unit)}")
        controller_name = controller_id.removeprefix("controller:")
        for enclosure in payload.get("enclosures") or []:
            enc_handle = str(enclosure.get("enc_handle") or "")
            if not enc_handle:
                continue
            enclosure_id = f"mpr-enclosure:{controller_id}:{enc_handle or enclosure.get('logical_id')}"
            enclosures[(controller_id, enc_handle)] = enclosure_id
            logical_key = _identifier_lookup_key(enclosure.get("logical_id"))
            if logical_key:
                enclosure_keys_by_handle[(controller_id, enc_handle)] = logical_key

        for expander in payload.get("expanders") or []:
            enc_handle = str(expander.get("enc_handle") or "")
            if not enc_handle:
                continue
            expander_id = f"expander:{controller_id}:{expander.get('sas_address') or expander.get('dev_handle')}"
            expanders[(controller_id, enc_handle)].append(expander_id)

        for device in payload.get("devices") or []:
            context = {
                "controller_id": controller_id,
                "controller_name": controller_name,
                "unit": unit,
                **device,
            }
            context["diagnostics"] = _diagnostics_for_mpr_device(
                diagnostics or {},
                controller_name,
                context,
            )
            enc_handle = str(device.get("enclosure_handle") or "")
            enclosure_key = enclosure_keys_by_handle.get((controller_id, enc_handle))
            context["enclosure_key"] = enclosure_key
            for candidate in _device_name_candidates(device.get("device")):
                key = (controller_name, candidate)
                # None latches contradictory provider rows instead of letting
                # the last row choose which bay receives their diagnostics.
                devices[key] = context if key not in devices or devices[key] == context else None
            device_target = _parse_mpr_target(device.get("target"))
            if device_target is not None and _is_mpr_disk_target(device.get("device")):
                target_key = (controller_name, device_target)
                devices_by_target[target_key] = (
                    context if target_key not in devices_by_target or devices_by_target[target_key] == context else None
                )
            device_slot = _parse_mpr_slot_number(device.get("slot"))
            # Provider-local handles still locate conflicting occupants when
            # the optional enclosure logical-ID probe is absent.
            if device_slot is not None and _is_mpr_disk_target(device.get("device")):
                handle_location = (controller_name, enc_handle, device_slot)
                devices_by_handle_location[handle_location] = (
                    context if handle_location not in devices_by_handle_location
                    or devices_by_handle_location[handle_location] == context else None
                )
            if enclosure_key and device_slot is not None and _is_mpr_disk_target(device.get("device")):
                location_key = (controller_name, enclosure_key, device_slot)
                devices_by_location[location_key] = (
                    context if location_key not in devices_by_location or devices_by_location[location_key] == context else None
                )

    return {
        "devices": devices,
        "devices_by_location": devices_by_location,
        "devices_by_handle_location": devices_by_handle_location,
        "devices_by_target": devices_by_target,
        "enclosure_keys_by_handle": enclosure_keys_by_handle,
        "enclosures": enclosures,
        "expanders": {key: _dedupe_strings(value) for key, value in expanders.items()},
    }


def _add_mpr_member_trace_context(
    *,
    nodes: dict[str, SasFabricNode],
    links: dict[str, SasFabricLink],
    trace_index: dict[str, Any],
    controller_name: str,
    path_id: str,
    bay_id: str,
    slot_number: int,
    path_state: str,
    member_device_name: str | None,
    member_target: int | None = None,
    member_target_rejected: bool = False,
    slot_enclosure_ids: list[str] | None = None,
    slot_location_numbers: list[int] | None = None,
    slot_ses_number: int | None = None,
    diagnostics: dict[str, Any] | None = None,
    own_evidence: bool = False,
) -> dict[str, Any]:
    device_context = _lookup_mpr_device_context(
        trace_index,
        controller_name,
        member_device_name,
        member_target=member_target,
        member_target_rejected=member_target_rejected,
        own_evidence=own_evidence,
        slot_enclosure_ids=slot_enclosure_ids,
        slot_location_numbers=slot_location_numbers,
        slot_ses_number=slot_ses_number,
    )
    if not device_context:
        return {}

    controller_id = str(device_context.get("controller_id") or f"controller:{controller_name}")
    try:
        unit = int(device_context.get("unit"))
    except (TypeError, ValueError):
        unit = -1
    evidence = _mpr_evidence(unit, "devices")
    diagnostic_summary = _diagnostic_metric_summary(
        device_context.get("diagnostics")
        or _diagnostics_for_member_device(
            diagnostics or {},
            member_device_name,
        )
    )
    enc_handle = str(device_context.get("enclosure_handle") or "")
    enclosure_id = trace_index.get("enclosures", {}).get((controller_id, enc_handle))
    expander_ids = list(trace_index.get("expanders", {}).get((controller_id, enc_handle), []))
    # Handles are controller-local and an enclosure may hold several
    # expanders: a joined row names only its own parent expander.
    parent = _mpr_handle(device_context.get("parent"))
    expander_ids = [node_id for node_id in expander_ids
                    if node_id in nodes and parent is not None
                    and _mpr_handle(nodes[node_id].raw.get("dev_handle")) == parent]
    if len(expander_ids) != 1:
        expander_ids = []

    node_ids: list[str] = []
    path_link_ids: list[str] = []
    bay_link_ids: list[str] = []
    for expander_id in expander_ids:
        existing_expander = nodes.get(expander_id)
        add_node(
            nodes,
            SasFabricNode(
                id=expander_id,
                kind="expander",
                label=existing_expander.label if existing_expander else expander_id,
                raw_id=existing_expander.raw_id if existing_expander else expander_id,
                controller_id=controller_id,
                related_slots=[slot_number],
                evidence=evidence,
            ),
        )
        node_ids.append(expander_id)
        link = add_link(
            links,
            path_id,
            expander_id,
            "path-expander",
            status=path_state,
            related_slots=[slot_number],
            evidence=evidence,
        )
        path_link_ids.append(link.id)
        bay_link_ids.append(link.id)
        if enclosure_id:
            enclosure_link = add_link(
                links,
                expander_id,
                enclosure_id,
                "expander-enclosure",
                related_slots=[slot_number],
                evidence=evidence,
            )
            path_link_ids.append(enclosure_link.id)
            bay_link_ids.append(enclosure_link.id)

    if enclosure_id:
        existing_enclosure = nodes.get(enclosure_id)
        add_node(
            nodes,
            SasFabricNode(
                id=enclosure_id,
                kind="mpr-enclosure",
                label=existing_enclosure.label if existing_enclosure else enclosure_id,
                raw_id=existing_enclosure.raw_id if existing_enclosure else enclosure_id,
                controller_id=controller_id,
                related_slots=[slot_number],
                evidence=evidence,
            ),
        )
        node_ids.append(enclosure_id)
        if not expander_ids:
            path_link = add_link(
                links,
                path_id,
                enclosure_id,
                "path-mpr-enclosure",
                status=path_state,
                related_slots=[slot_number],
                evidence=evidence,
            )
            path_link_ids.append(path_link.id)
            bay_link_ids.append(path_link.id)
        bay_link = add_link(
            links,
            enclosure_id,
            bay_id,
            "mpr-enclosure-bay",
            status=path_state,
            slot=slot_number,
            related_slots=[slot_number],
            evidence=evidence,
        )
        bay_link_ids.append(bay_link.id)

    return {
        "node_ids": _dedupe_strings(node_ids),
        "path_link_ids": _dedupe_strings(path_link_ids),
        "bay_link_ids": _dedupe_strings(bay_link_ids),
        "evidence": evidence,
        "metric": {
            "controller": controller_name,
            "state": path_state,
            "member_device_name": member_device_name,
            "mpr_device": device_context.get("device"),
            "sas_address": device_context.get("sas_address"),
            "handle": device_context.get("handle"),
            "parent": device_context.get("parent"),
            "speed": device_context.get("speed"),
            "enclosure_handle": enc_handle or None,
            "mpr_slot": device_context.get("slot"),
            "enclosure_id": enclosure_id,
            "expander_ids": expander_ids,
            "diagnostics": diagnostic_summary,
        },
    }


def _lookup_mpr_device_context(
    trace_index: dict[str, Any],
    controller_name: str,
    device_name: str | None,
    *,
    member_target: int | None = None,
    member_target_rejected: bool = False,
    own_evidence: bool = False,
    slot_enclosure_ids: list[str] | None = None,
    slot_location_numbers: list[int] | None = None,
    slot_ses_number: int | None = None,
) -> dict[str, Any] | None:
    devices = trace_index.get("devices", {})
    member_names = set(_device_name_candidates(device_name))
    enclosure_keys = {
        key for value in slot_enclosure_ids or [] if (key := _identifier_lookup_key(value))
    }
    for candidate in sorted(member_names):
        key = (controller_name, candidate)
        if key in devices and devices[key] is None:
            return None
        context = devices.get(key)
        if context:
            row_target = _parse_mpr_target(context.get("target"))
            if member_target_rejected or (member_target is not None and row_target is not None and row_target != member_target):
                return None
            if _mpr_target_rejected(context.get("target")):
                return None
            # A named member on this controller is independent identity evidence.
            # Only that match can establish SES N -> MPR N-1; never infer it
            # from a nameless neighbouring SAS target or a shared SAS address.
            if enclosure_keys and context.get("enclosure_key") and context["enclosure_key"] not in enclosure_keys:
                return None
            known_handles = {
                handle for (controller_id, handle), enclosure_key in trace_index.get("enclosure_keys_by_handle", {}).items()
                if controller_id == context["controller_id"] and enclosure_key in enclosure_keys
            }
            enc_handle = str(context.get("enclosure_handle") or "")
            if known_handles and enc_handle and enc_handle not in known_handles:
                return None
            location = _parse_mpr_slot_number(context.get("slot"))
            handle_location = (controller_name, enc_handle, location)
            handle_locations = trace_index.get("devices_by_handle_location", {})
            if member_target is None and handle_location in handle_locations and handle_locations[handle_location] is None:
                return None
            location_key = (controller_name, context.get("enclosure_key"), location)
            locations = trace_index.get("devices_by_location", {})
            # An exact name cannot override contradictory occupants at its own
            # provider location, including an independently proven SES offset.
            if member_target is None and location_key in locations and locations[location_key] is None:
                return None
            if slot_location_numbers and location not in slot_location_numbers:
                if slot_ses_number is None or slot_ses_number <= 0 or location != slot_ses_number - 1:
                    return None
            return context
    if member_target is not None:
        # CAM names this member's target on this controller: join by that
        # identity rather than by bay position (#936).
        target_key = (controller_name, member_target)
        by_target = trace_index.get("devices_by_target", {})
        if target_key in by_target:
            context = by_target[target_key]
            if context is None:
                return None
            if enclosure_keys and context.get("enclosure_key") and context["enclosure_key"] not in enclosure_keys:
                return None
            known_handles = {
                handle for (controller_id, handle), enclosure_key in trace_index.get("enclosure_keys_by_handle", {}).items()
                if controller_id == context["controller_id"] and enclosure_key in enclosure_keys
            }
            enc_handle = str(context.get("enclosure_handle") or "")
            if known_handles and enc_handle and enc_handle not in known_handles:
                return None
            named_occupants = set(_device_name_candidates(context.get("device")))
            if named_occupants and member_names and not named_occupants.intersection(member_names):
                return None
            location = _parse_mpr_slot_number(context.get("slot"))
            # Several ports of one disk share the bay coordinate. A unique
            # controller-local CAM target is stronger than that location.
            return context
    if own_evidence:
        # Direct paths, and a controller carrying several of this bay's paths,
        # require a readable matching target or an exact name.
        return None
    if member_target_rejected:
        # CAM printed a target for this member that is not a target number:
        # that is evidence, not absence, so position alone cannot join a row.
        return None
    devices_by_location = trace_index.get("devices_by_location", {})
    for enclosure_id in slot_enclosure_ids or []:
        enclosure_key = _identifier_lookup_key(enclosure_id)
        if not enclosure_key:
            continue
        for location_number in slot_location_numbers or []:
            location_key = (controller_name, enclosure_key, location_number)
            if location_key in devices_by_location and devices_by_location[location_key] is None:
                return None
            context = devices_by_location.get(location_key)
            if context:
                named_occupants = set(_device_name_candidates(context.get("device")))
                if named_occupants and member_names and not named_occupants.intersection(member_names):
                    return None
                # A row the controller assigns to another target is another
                # device, whatever its position.
                row_target = _parse_mpr_target(context.get("target"))
                if member_target is not None and row_target is not None and row_target != member_target:
                    return None
                # A printed target that is not a target number cannot be
                # compared, so it cannot be shown to be this member's.
                if row_target is None and _mpr_target_rejected(context.get("target")):
                    return None
                return context
    return None


def _diagnostics_for_mpr_device(
    diagnostics: dict[str, Any],
    controller_name: str,
    device_context: dict[str, Any],
) -> dict[str, Any]:
    target = normalize_text(device_context.get("target"))
    if not target:
        return {}
    return dict((diagnostics.get("by_controller_target") or {}).get(f"{controller_name}:{target}") or {})


def _diagnostics_for_member_device(diagnostics: dict[str, Any], member_device_name: str | None) -> dict[str, Any]:
    by_device = diagnostics.get("by_device") or {}
    for candidate in _device_name_candidates(member_device_name):
        summary = by_device.get(candidate)
        if summary:
            return dict(summary)
    return {}


def _diagnostic_metric_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not summary:
        return {}
    return {
        "event_count": summary.get("event_count", 0),
        "error_count": summary.get("error_count", 0),
        "retry_count": summary.get("retry_count", 0),
        "sense_count": summary.get("sense_count", 0),
        "ioc_terminated_count": summary.get("ioc_terminated_count", 0),
        "devices": summary.get("devices") or [],
        "targets": summary.get("targets") or [],
        "sense_counts": summary.get("sense_counts") or {},
        "loginfo_counts": summary.get("loginfo_counts") or {},
        "loginfo_rejected_count": summary.get("loginfo_rejected_count", 0),
        "fault_family_counts": summary.get("fault_family_counts") or {},
        "operation_counts": summary.get("operation_counts") or {},
        "top_findings": summary.get("top_findings") or [],
        "primary_fault": summary.get("primary_fault"),
        "operator_summary": summary.get("operator_summary"),
        "event_table": summary.get("event_table")
        or {
            "schema_version": 2,
            "total_count": 0,
            "page_size": 25,
            "sample_count": 0,
            "sample_limit": 25,
            "sample_kind": "recent",
            "truncated": False,
            "rows": [],
        },
    }


def _slot_enclosure_candidates(slot: SlotView) -> list[str]:
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    return _dedupe_strings(
        [
            raw_status.get("enclosure_id"),
            slot.enclosure_id,
            slot.enclosure_name,
            raw_status.get("enclosure_name"),
        ]
    )


def _slot_location_number_candidates(slot: SlotView) -> list[int]:
    """Exact bay coordinates only; SES numbers are not MPR locations.

    SES numbers bays one-based per shelf while an MPR location is zero-based per
    expander, so `ses_slot_number` is never a positional candidate: on a
    cascaded shelf it lands on the next bay (#936). A provider-named member can
    still prove a one-based conversion in `_lookup_mpr_device_context`, and a
    CAM target joins by identity there.
    """
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    if raw_status.get("manual_mapping_only") is True:
        # A manual-only bay (#1068): its index is not a validated position, so
        # it joins an MPR row by the mapped disk's identity only, never by place.
        return []
    exact: list[int] = [slot.slot]
    parsed = _parse_mpr_slot_number(raw_status.get("slot_number"))
    if parsed is not None and parsed not in exact:
        exact.append(parsed)
    return exact


def _parse_mpr_target(value: Any) -> int | None:
    """A target number is 1 to 9 ASCII digits; anything else is no target.

    `str.isdigit()` also admits other scripts' digits and characters such as
    a superscript two, which `int()` rejects with an exception, and any length.
    """
    text = normalize_text(str(value)) if value is not None else None
    if not text or not _MPR_TARGET_NUMBER.fullmatch(text):
        return None
    return int(text, 10)


_MPR_TARGET_NUMBER = re.compile(r"[0-9]{1,9}")


def _mpr_target_rejected(value: Any) -> bool:
    """A target was printed but is not a target number (unlike an absent one)."""
    text = normalize_text(str(value)) if value is not None else None
    return bool(text) and _parse_mpr_target(text) is None


def _cam_member_target_rejected(cam_devices: Any, controller_name: str, device_name: str | None) -> bool:
    """CAM printed a target for the member on this controller that is not admitted."""
    return any(
        normalize_text(cam_devices.controllers.get(candidate.lower())) == controller_name
        and _mpr_target_rejected(cam_devices.targets.get(candidate.lower()))
        for candidate in _device_name_candidates(device_name)
    )


def _cam_member_target(cam_devices: Any, controller_name: str, device_name: str | None) -> int | None:
    """The member's CAM target, only when CAM places it on this controller."""
    targets: set[int] = set()
    for candidate in _device_name_candidates(device_name):
        key = candidate.lower()
        if normalize_text(cam_devices.controllers.get(key)) != controller_name:
            continue
        target = _parse_mpr_target(cam_devices.targets.get(key))
        if target is not None:
            targets.add(target)
    return targets.pop() if len(targets) == 1 else None


def _device_name_candidates(*values: Any) -> list[str]:
    candidates: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        cleaned = normalize_text(text)
        if not cleaned:
            continue
        if cleaned.startswith("/dev/") and _looks_like_disk_device(cleaned[5:]):
            candidates.append(cleaned[5:])
        if _looks_like_disk_device(cleaned):
            candidates.append(cleaned)
        for token in re.split(r"[^A-Za-z0-9_.-]+", text):
            token_cleaned = normalize_text(token)
            if token_cleaned and _looks_like_disk_device(token_cleaned):
                candidates.append(token_cleaned)
    return _dedupe_strings(candidates)


def _looks_like_disk_device(value: str | None) -> bool:
    cleaned = normalize_text(value)
    if not cleaned:
        return False
    return bool(re.match(r"^(?:/dev/)?(?:da|ada|pass|mfid|nvd|nvme|sd|hd|xbd)\d", cleaned))


def _is_mpr_disk_target(value: str | None) -> bool:
    cleaned = (normalize_text(value) or "").lower()
    return "sas target" in cleaned or "sata target" in cleaned


def _identifier_lookup_key(value: Any) -> str | None:
    cleaned = normalize_text(str(value)) if value is not None else None
    if not cleaned:
        return None
    cleaned = cleaned.lower().removeprefix("0x")
    return cleaned or None


def _parse_mpr_slot_number(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    try:
        return int(match.group(0), 10)
    except ValueError:
        return None


def _mpr_evidence(unit: int, subcommand: str) -> list[str]:
    return [f"mprutil -u {unit} show {subcommand}"] if unit >= 0 else [f"mprutil show {subcommand}"]


def _mpr_expander_parent_context(
    expanders: list[dict[str, Any]], controller_id: str,
) -> list[tuple[str | None, str]]:
    """Admit only unique, level-consistent chains rooted at this controller.

    Handles are local to one MPR unit. A level-1 row proves direct attachment
    only when its parent is not another observed expander. Missing or conflicting
    evidence never becomes an HBA fallback, including for descendants.
    """
    handles = [str(row.get("dev_handle") or "").lower() for row in expanders]
    identities = [str(row.get("sas_address") or row.get("dev_handle") or "").lower() for row in expanders]
    handle_counts, identity_counts = Counter(handles), Counter(identities)
    by_handle = {handle: index for index, handle in enumerate(handles) if handle_counts[handle] == 1}
    levels = [
        int(value) if len(value) <= 10 and re.fullmatch(r"[0-9]+", value) else 0
        for row in expanders
        for value in [str(row.get("sas_level") or "")]
    ]
    result: list[tuple[str | None, str]] = [(None, "unknown") for _ in expanders]
    # Increasing levels make admission independent of source enumeration and
    # ensure a cycle cannot be admitted. No recursive traversal of source data.
    for index in sorted(range(len(expanders)), key=lambda item: levels[item]):
        row = expanders[index]
        parent = str(row.get("parent") or "").lower()
        if not handles[index] or handle_counts[handles[index]] != 1 or identity_counts[identities[index]] != 1:
            reason = "ambiguous_identity"
        elif levels[index] < 1:
            reason = "invalid_level"
        elif not parent:
            reason = "missing_parent"
        elif levels[index] == 1 and parent not in handle_counts:
            result[index] = (controller_id, "resolved")
            continue
        elif parent not in by_handle:
            reason = "unresolved_parent"
        else:
            parent_index = by_handle[parent]
            if levels[parent_index] != levels[index] - 1:
                reason = "contradictory_parent_level"
            elif result[parent_index][0] is None:
                reason = "upstream_unresolved"
            else:
                parent_row = expanders[parent_index]
                parent_id = f"expander:{controller_id}:{parent_row.get('sas_address') or parent_row.get('dev_handle')}"
                result[index] = (parent_id, "resolved")
                continue
        result[index] = (None, reason)
    return result


_MPR_EXPANDER_ATTACHED_DEVICES = frozenset({"SMP Target", "Edge Expander", "Fanout Expander", "Expander"})
_MPR_INITIATOR_ATTACHED_DEVICES = frozenset({"SAS Initiator"})
# A disk (an end device) and the enclosure processor, as the view's device
# table reads them (a JS test holds the two to agreement).
_MPR_DISK_ATTACHED_DEVICES = frozenset({"SAS Target", "SATA Target", "STP Target", "ATAPI Target"})
_MPR_SEP_ATTACHED_DEVICES = frozenset({"SEP Target"})
_MPR_REPORTED_LANE_BASIS = "reciprocal_reported_phy_rows"


def _mpr_phy_number(value: Any) -> int | None:
    # Parser rows carry decimal strings. Anything else is not PHY evidence.
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 5:
        return int(value)
    return None


def _mpr_handle(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{4}", value):
        return value.lower()
    return None


def _mpr_rate(value: Any) -> float | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,2})?", value):
        rate = float(value)
        return rate if rate > 0 else None
    return None


def _mpr_sas_address(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{16}", value) else None


def _mpr_endpoint_rows(rows: Any, *, peer_field: str) -> list[dict[str, Any]] | None:
    """Normalize one endpoint's PHY rows; None if any local PHY is unusable or reused."""
    if not isinstance(rows, list):
        return None
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        phy = _mpr_phy_number(row.get("phy"))
        if phy is None or phy in seen:
            return None
        seen.add(phy)
        device = row.get("device")
        normalized.append({
            "phy": phy,
            "peer": _mpr_handle(row.get(peer_field)),
            "remote": _mpr_phy_number(row.get("remote_phy")),
            "rate": _mpr_rate(row.get("speed")),
            "device": device.strip() if isinstance(device, str) else None,
            "controller_handle": _mpr_handle(row.get("controller_handle")),
        })
    return normalized


def _mpr_rows_by_peer(rows: list[dict[str, Any]] | None) -> dict[str | None, list[dict[str, Any]]] | None:
    if rows is None:
        return None
    grouped: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["peer"]].append(row)
    return dict(grouped)


class _MprReportedLaneContext:
    """Reported lanes for admitted MPR parent links, from one unit's PHY rows.

    A lane is published only when both endpoints report it: the child's row
    names the parent handle and parent PHY, and the parent's row names the child
    handle (and, for expanders, the child PHY). Any reused local PHY, a PHY
    another device also claims (on either endpoint), a type that contradicts the attachment, or
    unequal row sets leave the whole link null. Nothing here admits a link.
    """

    def __init__(self, expanders: list[dict[str, Any]], adapter_detail: Any, unit: int,
                 ambiguous_handles: set[str] | None = None) -> None:
        self._unit = unit
        # A handle naming more than one device of the unit names neither end.
        self._ambiguous = ambiguous_handles or set()
        self._expanders = expanders
        # Index once so each link costs O(its lanes), not a rescan of the unit.
        self._rows = [
            _mpr_rows_by_peer(_mpr_endpoint_rows(row.get("phys"), peer_field="dev_handle")) for row in expanders
        ]
        self._handles = [_mpr_handle(row.get("dev_handle")) for row in expanders]
        self._positions_by_handle: dict[str, list[int]] = defaultdict(list)
        for position, handle in enumerate(self._handles):
            if handle is not None:
                self._positions_by_handle[handle].append(position)
        adapter_rows = adapter_detail.get("phy_rows") if isinstance(adapter_detail, dict) else None
        self._adapter_rows = _mpr_rows_by_peer(_mpr_endpoint_rows(adapter_rows, peer_field="device_handle"))
        # (attached handle, attached PHY) claims across every expander row in
        # this unit, including rows of endpoints whose own table is unusable.
        self._claims: Counter[tuple[str, int]] = Counter()
        for row in expanders:
            for phy in row.get("phys") if isinstance(row.get("phys"), list) else []:
                if not isinstance(phy, dict):
                    continue
                peer, remote = _mpr_handle(phy.get("dev_handle")), _mpr_phy_number(phy.get("remote_phy"))
                if peer is not None and remote is not None:
                    self._claims[(peer, remote)] += 1

    def parent_link_lanes(self, index: int, *, to_controller: bool) -> SasFabricReportedLanes | None:
        child = self._expanders[index]
        child_handle, child_rows = self._handles[index], self._rows[index]
        parent_handle = _mpr_handle(child.get("parent"))
        if (child_handle is None or len(self._positions_by_handle[child_handle]) != 1
                or parent_handle is None or child_rows is None or child_handle == parent_handle
                or {child_handle, parent_handle} & self._ambiguous):
            return None
        if to_controller:
            parent_rows, parent_address = self._adapter_rows, None
            allowed = _MPR_INITIATOR_ATTACHED_DEVICES
        else:
            matches = self._positions_by_handle.get(parent_handle, [])
            if len(matches) != 1:
                return None
            parent_rows = self._rows[matches[0]]
            parent_address = _mpr_sas_address(self._expanders[matches[0]].get("sas_address"))
            allowed = _MPR_EXPANDER_ATTACHED_DEVICES
        if parent_rows is None:
            return None
        toward_parent = child_rows.get(parent_handle, [])
        toward_child = parent_rows.get(child_handle, [])
        if not toward_parent or len(toward_parent) != len(toward_child):
            return None
        # parent PHY -> (child PHY, child-reported rate)
        child_view: dict[int, tuple[int, float | None]] = {}
        for row in toward_parent:
            remote = row["remote"]
            if (remote is None or row["device"] not in allowed or remote in child_view
                    or self._claims[(parent_handle, remote)] != 1):
                return None
            child_view[remote] = (row["phy"], row["rate"])
        lanes: list[SasFabricReportedLane] = []
        for row in toward_child:
            if row["phy"] not in child_view:
                return None
            child_phy, child_rate = child_view[row["phy"]]
            if to_controller:
                # Adapter rows are not in _claims, so another expander's claim
                # on this child PHY is a contradiction, not a second lane.
                if row["controller_handle"] != parent_handle or self._claims[(child_handle, child_phy)] != 0:
                    return None
            elif (row["remote"] != child_phy or row["device"] not in _MPR_EXPANDER_ATTACHED_DEVICES
                  or self._claims[(child_handle, child_phy)] != 1):
                return None
            rate = row["rate"] if row["rate"] is not None and row["rate"] == child_rate else None
            lanes.append(SasFabricReportedLane(source_phy=row["phy"], target_phy=child_phy, negotiated_rate_gbps=rate))
        lanes.sort(key=lambda lane: lane.source_phy)
        rates = {lane.negotiated_rate_gbps for lane in lanes}
        evidence = [*_mpr_evidence(self._unit, "adapter"), *_mpr_evidence(self._unit, "expanders")] if to_controller \
            else _mpr_evidence(self._unit, "expanders")
        return SasFabricReportedLanes(
            basis=_MPR_REPORTED_LANE_BASIS,
            lane_count=len(lanes),
            source_phys=[lane.source_phy for lane in lanes],
            target_phys=sorted(lane.target_phy for lane in lanes),
            lanes=lanes,
            negotiated_rate_gbps=next(iter(rates)) if len(rates) == 1 else None,
            source_sas_address=parent_address,
            attached_sas_address=_mpr_sas_address(child.get("sas_address")),
            evidence=evidence,
        )


def _mpr_device_kind(device: Any) -> str:
    """What a device type says is attached, by the same table the view reads."""
    for kind, types in (("expander", _MPR_EXPANDER_ATTACHED_DEVICES), ("disk", _MPR_DISK_ATTACHED_DEVICES),
                        ("sep", _MPR_SEP_ATTACHED_DEVICES), ("initiator", _MPR_INITIATOR_ATTACHED_DEVICES)):
        if isinstance(device, str) and device.strip() in types:
            return kind
    return "other"


def _mpr_handle_devices(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The devices each handle of one MPR unit names, one entry per device.

    Every read list is evidence: the expander list, the device list and the
    enclosure list's enclosure processors. A device is its whole identity
    (kind, SAS address, parent handle): two records are one device only
    where the kinds agree and no field both read differs; a field one record
    does not read is not read, never a second device. The result is the
    same in any record order.
    """
    lists = payload.get("lists") or {}
    read = {"read", "empty"}
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add(handle: Any, kind: str, address: Any, parent: Any, listed: bool = False) -> None:
        key = _mpr_handle(handle)
        if key is not None and key != "0000":
            records[key].append({"kind": kind, "address": (_mpr_sas_address(address) or "").lower() or None,
                                 "parent": _mpr_handle(parent), "conflict": False, "listed": listed})

    # ``listed``: the expander list itself carries the device.
    if lists.get("expanders") in read:
        for row in payload.get("expanders") or []:
            add(row.get("dev_handle"), "expander", row.get("sas_address"), row.get("parent"), listed=True)
    if lists.get("devices") in read:
        for row in payload.get("devices") or []:
            add(row.get("handle"), _mpr_device_kind(row.get("device")), row.get("sas_address"), row.get("parent"))
    if lists.get("enclosures") in read:
        for row in payload.get("enclosures") or []:
            add(row.get("sep_handle"), "sep", None, None)
    devices: dict[str, list[dict[str, Any]]] = {}
    for handle, found in records.items():
        # Records with an address are grouped by kind and address; one
        # address with two parents is one device whose identity disagrees.
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for record in (item for item in found if item["address"]):
            held = grouped.setdefault((record["kind"], record["address"]), dict(record))
            if record["parent"] and held["parent"] and record["parent"] != held["parent"]:
                held["conflict"] = True
            held["parent"] = held["parent"] or record["parent"]
            held["listed"] = held["listed"] or record["listed"]
        named = list(grouped.values())
        # A record with no address is one of those devices where exactly its
        # kind and read parent fit; otherwise a device of its own.
        loose: list[dict[str, Any]] = []
        for record in (item for item in found if not item["address"]):
            fits = [item for item in named + loose if item["kind"] == record["kind"]
                    and (not record["parent"] or not item["parent"] or item["parent"] == record["parent"])]
            if not fits:
                loose.append(dict(record))
            for item in fits:
                item["listed"] = item["listed"] or record["listed"]
        devices[handle] = named + loose
    return devices


def _mpr_adapter_handle_states(rows: list[dict[str, Any]] | None, payload: dict[str, Any]) -> dict[str, str]:
    """Each adapter PHY whose device handle does not name exactly one expander of it.

    Handles are local to one MPR unit, so this is decided over the unit's
    whole expander, device and enclosure lists, before any enclosure
    selection. A handle naming more than one device, or one whose own
    records disagree, is ``ambiguous`` everywhere. A handle naming one
    device whose parent is not this adapter, or whose kind is not the kind
    the row reports, contradicts the row (``mismatch``). A list not read
    never relaxes that: two devices read are still ambiguous, and otherwise
    the handle is ``not_read``. Else the one device's kind: a disk
    (``end_device``), the enclosure processor (``sep``), an expander only the
    device list names (``unlisted_expander``) or another device
    (``other_device``); an expander the expander list carries, or a handle no
    list carries, has no state. The adapter rows stay as reported; the
    states are kept beside them, by PHY.
    """
    lists = payload.get("lists") or {}
    states: dict[str, str] = {}
    if not rows:
        return states
    # A list not read could carry another device with any handle: what is
    # read can still show two devices (ambiguous), never just one.
    unread = any(lists.get(name) not in {"read", "empty"} for name in ("expanders", "devices", "enclosures"))
    devices = _mpr_handle_devices(payload)
    for row in rows:
        handle = _mpr_handle(row.get("device_handle"))
        found = devices.get(handle or "", [])
        phy = str(row.get("phy"))
        if found and (len(found) > 1 or found[0]["conflict"]):
            states[phy] = "ambiguous"
            continue
        if unread and handle is not None and handle != "0000":
            states[phy] = "not_read"
            continue
        if not found:
            continue
        # 0000 is the utility's "no handle": the row's own handle not read.
        own = _mpr_handle(row.get("controller_handle"))
        kind, parent, own = found[0]["kind"], found[0]["parent"], None if own == "0000" else own
        # An HBA row reporting an expander names it by the initiator or an
        # expander type; any other kind must be the row's own.
        row_kind = _mpr_device_kind(row.get("device"))
        agrees = row_kind in {"initiator", "expander"} if kind == "expander" else row_kind == kind
        if (parent and own and parent != own) or not agrees:
            states[phy] = "mismatch"
        elif kind == "expander" and not found[0]["listed"]:
            # An expander only the device list names: not an absent one.
            states[phy] = "unlisted_expander"
        elif kind != "expander":
            states[phy] = {"disk": "end_device", "sep": "sep"}.get(kind, "other_device")
    return states


def _mpr_ambiguous_handles(payload: dict[str, Any]) -> set[str]:
    """Handles of one unit that name more than one device, or a device whose records disagree."""
    return {handle for handle, found in _mpr_handle_devices(payload).items() if len(found) > 1 or found[0]["conflict"]}


def _add_mpr_infrastructure(
    *,
    nodes: dict[str, SasFabricNode],
    links: dict[str, SasFabricLink],
    controllers: list[dict[str, Any]],
    unit_data: dict[int, dict[str, Any]],
    selected_enclosure_keys: set[str] | None = None,
) -> None:
    controller_ids_by_unit = _mpr_controller_ids_by_unit(controllers)
    selected_keys = selected_enclosure_keys or set()
    for unit, payload in sorted(unit_data.items()):
        controller_id = controller_ids_by_unit.get(unit, f"controller:{_mpr_controller_name_for_unit(unit)}")
        enclosure_counts = Counter(str(device.get("enclosure_handle") or "") for device in payload.get("devices") or [])
        # A device count is a measurement only from a device list that was read.
        devices_read = (payload.get("lists") or {}).get("devices") in {"read", "empty"}
        selected_enc_handles = {
            str(enclosure.get("enc_handle") or "")
            for enclosure in payload.get("enclosures") or []
            if _identifier_lookup_key(enclosure.get("logical_id")) in selected_keys
        }
        selected_enc_handles.discard("")
        expander_ids_by_enc_handle: dict[str, list[str]] = defaultdict(list)
        for controller in controllers:
            states = _mpr_adapter_handle_states(controller.get("phy_rows"), payload) if controller.get("id") == controller_id else {}
            if states:
                controller["device_handle_states"] = states
            if controller.get("id") == controller_id and payload.get("lists"):
                # Which replies were read, and every handle the unit's read
                # records show naming two devices, travel with the HBA itself.
                controller["mpr_lists"] = dict(payload["lists"])
                ambiguous = sorted(_mpr_ambiguous_handles(payload))
                if ambiguous:
                    controller["ambiguous_device_handles"] = ambiguous
        expanders = payload.get("expanders") or []
        parent_context = _mpr_expander_parent_context(expanders, controller_id)
        lane_context = _MprReportedLaneContext(expanders, payload.get("adapter_detail"), unit, _mpr_ambiguous_handles(payload))
        for index, (expander, (parent_id, parent_reason)) in enumerate(zip(expanders, parent_context, strict=True)):
            expander_id = f"expander:{controller_id}:{expander.get('sas_address') or expander.get('dev_handle')}"
            expander_key = _identifier_lookup_key(expander.get("sas_address"))
            enc_handle = str(expander.get("enc_handle") or "")
            selected_candidate = bool(
                selected_keys
                and (expander_key in selected_keys or enc_handle in selected_enc_handles)
            )
            raw = {
                "id": expander_id,
                "controller_id": controller_id,
                "unit": unit,
                "selected_enclosure_candidate": selected_candidate,
                "fabric_scope": "selected_enclosure" if selected_candidate else "unscoped",
                **expander,
                "parent_link_state": "resolved" if parent_id else "unknown",
                "parent_link_reason": parent_reason,
            }
            add_node(
                nodes,
                SasFabricNode(
                    id=expander_id,
                    kind="expander",
                    label=str(expander.get("sas_address") or expander.get("dev_handle") or "Expander"),
                    raw_id=str(expander.get("sas_address") or expander.get("dev_handle") or ""),
                    controller_id=controller_id,
                    metrics={
                        "linked_phys": expander.get("linked_phys"),
                        "num_phys": expander.get("num_phys"),
                        "sas_level": expander.get("sas_level"),
                        # The expander header was read, so a missing count is a PHY table not read whole.
                        **({"not_read": ["linked_phys"]} if expander.get("linked_phys") is None else {}),
                    },
                    evidence=[f"mprutil -u {unit} show expanders"] if unit >= 0 else ["mprutil show expanders"],
                    raw=raw,
                ),
            )
            if parent_id:
                parent_link = add_link(
                    links, parent_id, expander_id,
                    "controller-expander" if parent_id == controller_id else "expander-expander",
                    evidence=_mpr_evidence(unit, "expanders"),
                )
                lanes = lane_context.parent_link_lanes(index, to_controller=parent_id == controller_id)
                # One admitted parent link per child. A second, different
                # report for the same link is contradictory, not a choice.
                if parent_link.reported_lanes is not None and parent_link.reported_lanes != lanes:
                    lanes = None
                parent_link.reported_lanes = lanes
            if enc_handle:
                expander_ids_by_enc_handle[enc_handle].append(expander_id)

        for enclosure in payload.get("enclosures") or []:
            enc_handle = str(enclosure.get("enc_handle") or "")
            enclosure_id = f"mpr-enclosure:{controller_id}:{enc_handle or enclosure.get('logical_id')}"
            enclosure_key = _identifier_lookup_key(enclosure.get("logical_id"))
            selected_candidate = bool(
                selected_keys
                and (enclosure_key in selected_keys or enc_handle in selected_enc_handles)
            )
            raw = {
                "id": enclosure_id,
                "controller_id": controller_id,
                "unit": unit,
                "device_count": int(enclosure_counts.get(enc_handle, 0)) if devices_read else None,
                "selected_enclosure_candidate": selected_candidate,
                "fabric_scope": "selected_enclosure" if selected_candidate else "unscoped",
                **enclosure,
            }
            add_node(
                nodes,
                SasFabricNode(
                    id=enclosure_id,
                    kind="mpr-enclosure",
                    label=str(enclosure.get("logical_id") or enc_handle or "MPR enclosure"),
                    raw_id=str(enclosure.get("logical_id") or enc_handle or ""),
                    controller_id=controller_id,
                    metrics={
                        "slots": enclosure.get("slots"),
                        "device_count": raw["device_count"],
                        **({} if devices_read else {"not_read": ["device_count"]}),
                    },
                    evidence=[f"mprutil -u {unit} show enclosures"] if unit >= 0 else ["mprutil show enclosures"],
                    raw=raw,
                ),
            )
            for expander_id in expander_ids_by_enc_handle.get(enc_handle, []):
                add_link(links, expander_id, enclosure_id, "expander-enclosure")


_SMP_COUNTER_EVIDENCE = "camcontrol smpcmd REPORT PHY ERROR LOG"


def _smp_output_span(command: str) -> int:
    """How many PHYs an output key's range covers; the one-PHY probe sorts first."""
    match = re.search(r":([0-9a-f]{2})-([0-9a-f]{2})$", command)
    return int(match.group(2), 16) - int(match.group(1), 16) if match else -1


def _merged_phy_error_log_rows(
    outputs: dict[str, str],
    routed: dict[tuple[int, str], Any],
    kept: Mapping[tuple[int, str], KeptCounterRead],
) -> dict[tuple[int, str], dict[int, dict[str, Any]]]:
    """Counter rows per (unit, expander address).

    Only output read through the pass device this refresh's topology routes
    to that expander counts, and each output is validated against the key it
    was asked for (parse_chunk_output()): oversized or malformed output reads
    as ``rejected_output``, never as counters. An expander with a kept
    reading takes its full-read rows from it (the collector keeps exactly
    what it read, decoded) and only the one-PHY probe from this refresh's
    output. A measured row wins over an unknown one, and when both the probe
    and the full read measured a PHY, the full read wins.
    """
    merged: dict[tuple[int, str], dict[int, dict[str, Any]]] = {}

    def add(key: tuple[int, str], rows: Iterable[dict[str, Any]]) -> None:
        current = merged.setdefault(key, {})
        for row in rows:
            existing = current.get(row["phy"])
            if existing is None or existing.get("counter_state") != "measured" or row["counter_state"] == "measured":
                current[row["phy"]] = row

    smp_outputs = [
        (command, stdout) for command, stdout in outputs.items()
        if command.startswith(SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX)
    ]
    for command, stdout in sorted(smp_outputs, key=lambda item: (_smp_output_span(item[0]), item[0])):
        parsed = parse_chunk_output(command, stdout)
        if parsed is None or parsed.unit is None or parsed.expander_sas_address is None:
            continue
        key = (parsed.unit, parsed.expander_sas_address)
        target = routed.get(key)
        if target is None or target.device != parsed.device:
            continue
        if key in kept and _smp_output_span(command) != 0:
            continue
        add(key, parsed.rows)
    for key, read in kept.items():
        add(key, read.row_dicts())
    return merged


def _kept_counter_reads(
    reads: Mapping[tuple[int, str], KeptCounterRead] | None,
    routed: dict[tuple[int, str], Any],
) -> dict[tuple[int, str], KeptCounterRead]:
    """Kept readings that count this refresh: the pass device this refresh's
    topology routes to that expander must be the one they were read through."""
    kept: dict[tuple[int, str], KeptCounterRead] = {}
    for key, read in (reads or {}).items():
        target = routed.get(key)
        if isinstance(read, KeptCounterRead) and target is not None and target.device == read.device:
            kept[key] = read
    return kept


def _counter_read_stop(
    read: KeptCounterRead | None, rows: Sequence[Mapping[str, Any]],
) -> tuple[str, int] | None:
    """Where and why the latest read of an expander ended early, if it did."""
    if read is not None and read.stopped is not None:
        return read.stopped[0], read.stopped[1]
    for row in rows:
        if row.get("counter_reason") in {"no_response", "rejected_output"}:
            return str(row["counter_reason"]), int(row["phy"])
    return None


def _attach_expander_phy_error_counters(
    nodes: dict[str, SasFabricNode],
    outputs: dict[str, str],
    capability: str | None,
    reads: Mapping[tuple[int, str], KeptCounterRead] | None = None,
) -> None:
    """Attach SMP REPORT PHY ERROR LOG counters to CORE expander PHY rows (#941).

    Counters the app could not read stay None with a reason, never zero. A
    refused sudo grant reads as "not available (permission not granted)".
    Counters kept from an earlier refresh carry their read time; they are not
    shown once sudo refuses the read. A read that ended early (the expander
    did not answer, or its answer was rejected) says where it stopped.
    """
    expander_nodes = [node for node in nodes.values() if node.kind == "expander"]
    if not expander_nodes:
        return
    routed = {(target.unit, target.expander_sas_address): target for target in discover_smp_targets(outputs)}
    kept = {} if capability == SMP_CAPABILITY_DENIED else _kept_counter_reads(reads, routed)
    results = _merged_phy_error_log_rows(outputs, routed, kept)
    # More routed expanders or PHYs than one refresh may read: the unread
    # ones wait their turn in the rotation; they are not a routing failure.
    budget_limited = bool(results) and (
        len(routed) > SMP_MAX_TARGETS_PER_REFRESH
        or sum(min(target.num_phys, 255) for target in routed.values()) > SMP_MAX_CALLS_PER_REFRESH - 1
    )
    for node in expander_nodes:
        unit = node.raw.get("unit")
        key = (unit if isinstance(unit, int) else -1, str(node.raw.get("sas_address") or "").lower())
        target = routed.get(key)
        read = kept.get(key)
        rows = [results[key][phy] for phy in sorted(results.get(key, {}))]
        states = {row["counter_state"] for row in rows}
        stop = _counter_read_stop(read, rows)
        reason = None
        if "measured" in states:
            state, message = "available", None
            reason = stop[0] if stop else None
        elif (rows and states == {SMP_CAPABILITY_DENIED}) or (not rows and capability == SMP_CAPABILITY_DENIED):
            state, message = "permission_denied", SMP_PERMISSION_DENIED_MESSAGE
        elif target is None and not rows:
            state, message = "no_route", SMP_NO_ROUTE_MESSAGE
        elif rows or stop:
            state, message = "unavailable", SMP_NO_ANSWER_MESSAGE
            if stop and stop[0] == "no_response":
                message, reason = SMP_HUNG_MESSAGE, stop[0]
            elif stop and stop[0] == "rejected_output":
                message, reason = SMP_REJECTED_OUTPUT_MESSAGE, stop[0]
            elif stop:
                reason = stop[0]
        elif budget_limited:
            state, message, reason = "not_read", SMP_READ_BUDGET_MESSAGE, SMP_READ_BUDGET_REASON
        else:
            state, message = "not_read", SMP_NOT_READ_MESSAGE
        if state != "available" and state != "unavailable":
            stop = None
        by_phy = {row["phy"]: row for row in rows}
        phys: list[dict[str, Any]] = []
        unread = 0
        for phy_row in node.raw.get("phys") or []:
            copied = dict(phy_row)
            try:
                number = int(str(copied.get("phy")))
            except ValueError:
                number = None
            counters = by_phy.get(number) if number is not None else None
            if counters is not None:
                copied.update({field: counters.get(field) for field in SMP_COUNTER_FIELDS})
                copied["counter_state"] = counters["counter_state"]
                copied["counter_reason"] = counters["counter_reason"]
            else:
                # A read can stop early (no answer); the PHYs after it were
                # not read, so they are unknown rather than missing.
                copied.update({field: None for field in SMP_COUNTER_FIELDS})
                copied["counter_state"] = "unknown" if state == "available" else state
                copied["counter_reason"] = "not_read" if state == "available" else reason
                if state == "available":
                    unread += 1
            phys.append(copied)
        if phys:
            node.raw["phys"] = phys
        summary = smp_counter_summary(rows)
        summary["unknown_phys"] += unread
        read_at = read.read_at if read is not None else None
        node.raw["phy_error_counters"] = {
            "state": state,
            "message": message,
            "reason": reason,
            "stopped_at_phy": stop[1] if stop else None,
            "source": _SMP_COUNTER_EVIDENCE,
            "device": target.device if target else None,
            "read_at": read_at,
            **summary,
            "rows": rows,
        }
        if state == "available":
            parts = [f"{summary['measured_phys']} PHYs read", f"{summary['phys_with_errors']} with errors"]
            if summary["unknown_phys"]:
                parts.append(f"{summary['unknown_phys']} unknown")
            stop_text = f"stopped at PHY {stop[1]} ({SMP_STOP_REASON_LABELS.get(stop[0], stop[0])})" if stop else ""
            later = read is not None and read.stopped is not None and read.stopped[2] != read_at
            if stop and not later:
                parts.append(f"read {stop_text}")
            if read_at:
                parts.append(f"read at {read_at}")
            if stop and later:
                # The rows are from an earlier read; the latest one stopped.
                parts.append(f"a later read {stop_text} at {read.stopped[2]}")
            node.metrics["phy_error_counters"] = ", ".join(parts)
        else:
            node.metrics["phy_error_counters"] = message
        if rows:
            node.evidence = _dedupe_strings([*node.evidence, _SMP_COUNTER_EVIDENCE])


def _scope_mpr_infrastructure(
    *,
    nodes: dict[str, SasFabricNode],
    links: dict[str, SasFabricLink],
    traces: dict[str, SasFabricTrace],
    snapshot: InventorySnapshot,
) -> list[str]:
    selected_bay_slots = _snapshot_slot_numbers(snapshot.slots)
    selected_disk_slots = _snapshot_disk_slot_numbers(snapshot.slots)
    scoped_node_ids: set[str] = set()
    controller_slot_updates: dict[str, list[int]] = defaultdict(list)
    expanders = [node for node in nodes.values() if node.kind == "expander"]
    # Include selected-scope seeding in the budget before copying any bay list.
    seed_memberships = sum(len(node.related_slots) if node.related_slots else
                           len(selected_bay_slots) if node.raw.get("selected_enclosure_candidate") else 0
                           for node in expanders)
    limited = len(expanders) > _facade._MAX_MPR_ANCESTRY_NODES or seed_memberships > _facade._MAX_MPR_ANCESTRY_MEMBERSHIPS

    for node in list(nodes.values()):
        if node.kind not in {"expander", "mpr-enclosure"}:
            continue
        if not node.raw.get("selected_enclosure_candidate"):
            continue
        if limited and node.kind == "expander":
            continue
        scoped_node_ids.add(node.id)
        if not node.related_slots and selected_bay_slots:
            node.related_slots = list(selected_bay_slots)
        node.metrics.update(
            {
                "selected_enclosure": True,
                "selected_bay_count": len(selected_bay_slots),
                "selected_disk_count": len(selected_disk_slots),
                "selected_disk_slots": selected_disk_slots,
            }
        )
        node.raw.update(
            {
                "selected_enclosure_candidate": True,
                "selected_bay_count": len(selected_bay_slots),
                "selected_disk_count": len(selected_disk_slots),
                "selected_disk_slots": selected_disk_slots,
            }
        )
        if node.controller_id:
            controller_slot_updates[node.controller_id].extend(node.related_slots)

    # Keep verified upstream context for selected expanders, not their siblings
    # or enclosures. Related bays describe reachability here, not bay identity
    # or membership in the selected enclosure.
    upstream_by_target = {
        link.target: link.source for link in links.values() if link.kind == "expander-expander"
    }
    context_node_ids = set(scoped_node_ids)
    # Parent links are already admitted as unique, level-consistent chains by
    # _mpr_expander_parent_context. Process children before parents so each
    # edge contributes its complete slot set once, not once per arriving leaf.
    membership_count = sum(len(node.related_slots) for node in expanders)
    slot_sets = {} if limited else {node.id: set(node.related_slots) for node in expanders}
    child_counts = dict.fromkeys(slot_sets, 0)
    for child_id, parent_id in upstream_by_target.items():
        if child_id in slot_sets and parent_id in slot_sets:
            child_counts[parent_id] += 1
    pending = [node_id for node_id, count in child_counts.items() if count == 0]
    updated_node_ids: set[str] = set()
    while pending:
        child_id = pending.pop()
        parent_id = upstream_by_target.get(child_id)
        if parent_id not in slot_sets:
            continue
        if slot_sets[child_id]:
            additions = slot_sets[child_id] - slot_sets[parent_id]
            if membership_count + len(additions) > _facade._MAX_MPR_ANCESTRY_MEMBERSHIPS:
                limited = True
                break
            membership_count += len(additions)
            slot_sets[parent_id].update(additions)
            context_node_ids.add(parent_id)
            updated_node_ids.add(parent_id)
        child_counts[parent_id] -= 1
        if child_counts[parent_id] == 0:
            pending.append(parent_id)
    if limited:
        # Staged memberships have not reached nodes. Retain direct observations,
        # but no partial ancestry can imply verified controller connectivity.
        context_node_ids = set(scoped_node_ids)
        for node in expanders:
            node.raw.update(parent_link_state="unknown", parent_link_reason="resource_limit")
        for link_id, link in list(links.items()):
            if link.kind in {"controller-expander", "expander-expander"}:
                links.pop(link_id)
    else:
        for node_id in updated_node_ids:
            nodes[node_id].related_slots = sorted(slot_sets[node_id])

    for controller_id, slots in controller_slot_updates.items():
        controller = nodes.get(controller_id)
        if not controller:
            continue
        controller.related_slots = _dedupe_ints([*controller.related_slots, *slots])
        controller.metrics["selected_enclosure"] = True
        controller.metrics["selected_bay_count"] = len(selected_bay_slots)
        controller.metrics["selected_disk_count"] = len(selected_disk_slots)

    for link in links.values():
        source = nodes.get(link.source)
        target = nodes.get(link.target)
        related_slots = _dedupe_ints([*(source.related_slots if source else []), *(target.related_slots if target else [])])
        if not link.related_slots and (link.source in context_node_ids or link.target in context_node_ids):
            link.related_slots = related_slots or list(selected_bay_slots)
        if link.kind == "host-controller" and link.target in controller_slot_updates:
            link.related_slots = _dedupe_ints([*link.related_slots, *controller_slot_updates[link.target]])

    drop_ids = {
        node.id
        for node in nodes.values()
        if node.kind in {"expander", "mpr-enclosure"}
        and not node.related_slots
        and not node.raw.get("selected_enclosure_candidate")
    }
    for node_id in drop_ids:
        nodes.pop(node_id, None)
    for link_id, link in list(links.items()):
        if link.source in drop_ids or link.target in drop_ids:
            links.pop(link_id, None)

    scoped_controller_ids = {
        node.controller_id
        for node_id in context_node_ids
        for node in [nodes.get(node_id)]
        if node and node.controller_id
    }
    scoped_link_ids = [
        link.id
        for link in links.values()
        if (link.source in context_node_ids and link.target in context_node_ids)
        or (link.source in scoped_controller_ids and link.target in context_node_ids)
        or (link.kind == "host-controller" and link.target in scoped_controller_ids)
    ]
    scoped_trace_node_ids = _dedupe_strings(["host", *sorted(scoped_controller_ids), *sorted(context_node_ids)])
    for trace in traces.values():
        if trace.kind != "bay" or not slots_overlap(trace.slots, selected_bay_slots):
            continue
        if any(node_id.startswith(("expander:", "mpr-enclosure:")) for node_id in trace.node_ids):
            continue
        trace.node_ids = _dedupe_strings([*trace.node_ids, *scoped_trace_node_ids])
        trace.link_ids = _dedupe_strings([*trace.link_ids, *scoped_link_ids])
        trace.evidence = _dedupe_strings([*trace.evidence, "selected enclosure scope"])
        trace.metrics["selected_enclosure_scope"] = True

    # Existing member-derived bay/path traces need only their verified upstream
    # chain, never sibling expanders or the ancestors' other enclosures.
    parent_links = {link.target: link for link in links.values()
                    if link.kind in {"controller-expander", "expander-expander", "host-controller"}}
    for trace in traces.values():
        trace_nodes = set(trace.node_ids)
        trace_links = set(trace.link_ids)
        pending_nodes = list(trace_nodes)
        while pending_nodes:
            link = parent_links.get(pending_nodes.pop())
            if link is None:
                continue
            trace_links.add(link.id)
            if link.source not in trace_nodes:
                trace_nodes.add(link.source)
                pending_nodes.append(link.source)
        trace.node_ids = [node_id for node_id in _dedupe_strings([*trace.node_ids, *sorted(trace_nodes)]) if node_id in nodes]
        trace.link_ids = [link_id for link_id in _dedupe_strings([*trace.link_ids, *sorted(trace_links)]) if link_id in links
                          and links[link_id].source in trace_nodes and links[link_id].target in trace_nodes]
    return ["MPR expander ancestry resource limit exceeded; upstream connectivity is unknown."] if limited else []
