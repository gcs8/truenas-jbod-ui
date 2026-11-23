from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import quote, unquote

from app.config import SystemConfig
from app.models.domain import (
    InventorySnapshot,
    MultipathMember,
    SasFabricAlias,
    SasFabricLink,
    SasFabricNode,
    SasFabricSnapshot,
    SasFabricTrace,
    SourceStatus,
    SlotView,
    slot_occupancy,
)
from app.services.core_expander_counters import KeptCounterRead
from app.services.parsers import (
    bay_peer_devices, canonicalize_ssh_command, normalize_device_name, normalize_text, parse_camcontrol_devlist,
)
from app.services.sas_fabric_parsers import (
    CORE_MPRUTIL_UNIT_SUBCOMMANDS as CORE_MPRUTIL_UNIT_SUBCOMMANDS,
    MPR_REJECTED_CONTROLLER_REASON,
    _freebsd_pci_location_to_address as _freebsd_pci_location_to_address,
    _mpr_dmesg_event_type as _mpr_dmesg_event_type,
    _mpr_dmesg_severity as _mpr_dmesg_severity,
    _mpr_unit_from_text,
    _parse_mpr_sense_message as _parse_mpr_sense_message,
    _pciconf_bus_address as _pciconf_bus_address,
    _split_mpr_dmesg_timestamp as _split_mpr_dmesg_timestamp,
    _strip_syslog_kernel_prefix as _strip_syslog_kernel_prefix,
    admit_mpr_unit_number,
    build_core_mprutil_unit_commands as build_core_mprutil_unit_commands,
    discover_mpr_units_from_adapter_summary,
    MAX_CORE_MPR_SUMMARY_UNITS,
    mpr_summary_units_over_limit,
    rejected_mpr_summary_rows,
    rejected_mpr_expander_headers,
    rejected_mpr_summary_unit_tokens,
    parse_dmidecode_slots,
    parse_mpr_adapter_detail,
    parse_mpr_adapter_summary,
    parse_mpr_device_list,
    parse_mpr_devices as parse_mpr_devices,
    parse_mpr_dmesg_events,
    parse_mpr_enclosures,
    parse_mpr_expanders,
    parse_mpr_iocfacts,
    parse_mpr_sysctl_locations,
    parse_pciconf_sas_controllers,
)
# The CORE projection moved to sas_fabric_core. The names below are used only there,
# and stay importable from this module.
from typing import Sequence  # noqa: F401
from app.models.domain import SasFabricReportedLane, SasFabricReportedLanes  # noqa: F401
from app.services.core_expander_counters import (  # noqa: F401
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
    SMP_MAX_CALLS_PER_REFRESH,
    SMP_MAX_TARGETS_PER_REFRESH,
    SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX,
    counter_summary as smp_counter_summary,
    discover_smp_targets,
    parse_chunk_output,
)
from app.services import sas_fabric_core as _sas_fabric_core
from app.services.sas_fabric_core import (
    _MPR_EXPANDER_ATTACHED_DEVICES as _MPR_EXPANDER_ATTACHED_DEVICES,
    _MPR_INITIATOR_ATTACHED_DEVICES as _MPR_INITIATOR_ATTACHED_DEVICES,
    _MPR_REPORTED_LANE_BASIS as _MPR_REPORTED_LANE_BASIS,
    _MprReportedLaneContext as _MprReportedLaneContext,
    _SMP_COUNTER_EVIDENCE as _SMP_COUNTER_EVIDENCE,
    _add_mpr_infrastructure,
    _add_mpr_member_trace_context,
    _attach_expander_phy_error_counters,
    _build_mpr_trace_index,
    _cam_member_target,
    _cam_member_target_rejected,
    _counter_read_stop as _counter_read_stop,
    _device_name_candidates as _device_name_candidates,
    _diagnostic_metric_summary,
    _diagnostics_for_member_device as _diagnostics_for_member_device,
    _diagnostics_for_mpr_device as _diagnostics_for_mpr_device,
    _identifier_lookup_key,
    _is_mpr_disk_target as _is_mpr_disk_target,
    _kept_counter_reads as _kept_counter_reads,
    _looks_like_disk_device as _looks_like_disk_device,
    _lookup_mpr_device_context as _lookup_mpr_device_context,
    _merged_phy_error_log_rows as _merged_phy_error_log_rows,
    _mpr_endpoint_rows as _mpr_endpoint_rows,
    _mpr_evidence,
    _mpr_expander_parent_context as _mpr_expander_parent_context,
    _mpr_handle as _mpr_handle,
    _mpr_phy_number as _mpr_phy_number,
    _mpr_rate as _mpr_rate,
    _mpr_rows_by_peer as _mpr_rows_by_peer,
    _mpr_sas_address as _mpr_sas_address,
    _parse_mpr_slot_number,
    _parse_mpr_target as _parse_mpr_target,
    _scope_mpr_infrastructure,
    _slot_enclosure_candidates,
    _slot_location_number_candidates,
    _smp_output_span as _smp_output_span,
)

_sas_fabric_core.bind_facade(sys.modules[__name__])


SAS_FABRIC_CANONICAL_ALIAS_SOURCE = "operator-canonical-v1"
CORE_MESSAGES_TAIL_COMMAND = "tail -n 4000 /var/log/messages"
CORE_MESSAGES_TAIL_SUDO_COMMAND = "sudo -n /usr/bin/tail -n 4000 /var/log/messages"
CORE_MPR_DMESG_EVENTS_COMMAND = (
    "messages=$({ "
    f"{CORE_MESSAGES_TAIL_COMMAND} 2>/dev/null || "
    f"{CORE_MESSAGES_TAIL_SUDO_COMMAND} 2>/dev/null || true; "
    "} | egrep '(mpr[0-9]+:|\\(da[0-9]+:mpr[0-9]+:)' || true); "
    "if [ -n \"$messages\" ]; then printf '%s\\n' \"$messages\" | tail -n 400; "
    "else dmesg -a | egrep '(mpr[0-9]+:|\\(da[0-9]+:mpr[0-9]+:)' | tail -n 400; fi"
)
CORE_PCICONF_LV_COMMAND = "/usr/sbin/pciconf -lv"
CORE_PCICONF_LV_OPTIONAL_COMMAND = f"{CORE_PCICONF_LV_COMMAND} 2>/dev/null || true"
CORE_DMIDECODE_SLOT_COMMAND = "sudo -n /usr/local/sbin/dmidecode -t slot"
CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND = f"{CORE_DMIDECODE_SLOT_COMMAND} 2>/dev/null || true"
CORE_MPR_SYSCTL_LOCATION_COMMAND = (
    "sysctl -a 2>/dev/null | egrep '^dev\\.mpr\\.[0-9]+\\.%(location|parent):' || true"
)

_INVENTORY_SNAPSHOT_EVIDENCE = ("inventory snapshot",)
_PROFILE_SLOT_LAYOUT_EVIDENCE = ("profile slot layout",)
_SLOT_MULTIPATH_EVIDENCE = ("multipath members",)
_LINUX_SES_PATH_EVIDENCE = (
    "lsscsi -g",
    "lsscsi -g -t",
    "sg_ses AES/EC",
    "sg_ses --join --filter",
)
_LINUX_SES_ENCLOSURE_EVIDENCE = ("sg_ses AES/EC", "sg_ses --join --filter")
_LINUX_SES_BAY_EVIDENCE = (
    "inventory snapshot",
    "lsblk --json",
    "lsscsi -g -t",
    "sg_ses AES/EC",
    "sg_ses --join --filter",
)
_SG_SES_AES_EVIDENCE = ("sg_ses AES/EC",)
_BMC_INVENTORY_EVIDENCE = ("BMC inventory",)

# One retained ancestry membership is repeated on links, payloads and traces.
# Reject the complete expansion on overflow, never publish a partial path.
_MAX_MPR_ANCESTRY_NODES = 4096
_MAX_MPR_ANCESTRY_MEMBERSHIPS = 16384


def _select_sas_fabric_builder_key(system: SystemConfig, snapshot: InventorySnapshot) -> str:
    platform = system.truenas.platform
    if platform in {"scale", "linux"} and _snapshot_has_linux_ses_evidence(snapshot):
        return "linux_ses"
    if platform != "core":
        return "platform_storage"
    return "core_mpr"


@dataclass(frozen=True)
class SasFabricBuildContext:
    system: SystemConfig
    snapshot: InventorySnapshot
    normalized_outputs: dict[str, str]
    sources: dict[str, SourceStatus] | None
    warnings: list[str]
    command_failures: list[dict[str, Any]] | None
    aliases: list[SasFabricAlias] | None
    alias_map: dict[str, SasFabricAlias]
    # CORE expander PHY error counter capability last seen by the collector
    # (#941): "granted", "permission_denied" or None when not yet known.
    expander_counter_capability: str | None = None
    # Kept counter readings by (unit, expander SAS address): decoded rows
    # with their UTC read time, shown until the expander is due for its next
    # read (#1088 review).
    expander_counter_reads: Mapping[tuple[int, str], KeptCounterRead] | None = None
    # Lookup keys of every API disk the inventory read, including unplaced
    # disks and disks on other enclosures (#1471). Process-local input, never
    # part of the snapshot or the fabric payload. None means unknown.
    disk_lookup_keys: tuple[frozenset[str], ...] | None = None


class SasFabricBuilder(Protocol):
    key: str

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        ...


class CoreMprFabricBuilder:
    key = "core_mpr"

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        return _build_core_mpr_fabric_snapshot(context)


class LinuxSesFabricBuilder:
    key = "linux_ses"

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        return _build_linux_ses_fabric_snapshot(context)


class PlatformStorageFabricBuilder:
    key = "platform_storage"

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        return _build_platform_storage_fabric_snapshot(context)


_SAS_FABRIC_BUILDERS: tuple[SasFabricBuilder, ...] = (
    CoreMprFabricBuilder(),
    LinuxSesFabricBuilder(),
    PlatformStorageFabricBuilder(),
)
_SAS_FABRIC_BUILDER_REGISTRY: dict[str, SasFabricBuilder] = {
    builder.key: builder for builder in _SAS_FABRIC_BUILDERS
}


def _build_sas_fabric_context(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    ssh_outputs: dict[str, str],
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    command_failures: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
    expander_counter_capability: str | None = None,
    expander_counter_reads: Mapping[tuple[int, str], KeptCounterRead] | None = None,
    disk_lookup_keys: Iterable[Iterable[str]] | None = None,
) -> SasFabricBuildContext:
    return SasFabricBuildContext(
        system=system,
        snapshot=snapshot,
        normalized_outputs=_canonical_outputs(ssh_outputs),
        sources=sources,
        warnings=list(warnings or []),
        command_failures=command_failures,
        aliases=aliases,
        alias_map=_sas_fabric_alias_map(aliases or []),
        expander_counter_capability=expander_counter_capability,
        expander_counter_reads=expander_counter_reads,
        disk_lookup_keys=(
            None if disk_lookup_keys is None else tuple(frozenset(keys) for keys in disk_lookup_keys)
        ),
    )


def _select_sas_fabric_builder(context: SasFabricBuildContext) -> SasFabricBuilder:
    builder_key = _select_sas_fabric_builder_key(context.system, context.snapshot)
    try:
        return _SAS_FABRIC_BUILDER_REGISTRY[builder_key]
    except KeyError as exc:
        raise RuntimeError(f"No SAS fabric builder registered for {builder_key!r}") from exc


def build_sas_fabric_snapshot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    ssh_outputs: dict[str, str],
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    command_failures: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
    expander_counter_capability: str | None = None,
    expander_counter_reads: Mapping[tuple[int, str], KeptCounterRead] | None = None,
    disk_lookup_keys: Iterable[Iterable[str]] | None = None,
) -> SasFabricSnapshot:
    context = _build_sas_fabric_context(
        system=system,
        snapshot=snapshot,
        ssh_outputs=ssh_outputs,
        sources=sources,
        warnings=warnings,
        command_failures=command_failures,
        aliases=aliases,
        expander_counter_capability=expander_counter_capability,
        expander_counter_reads=expander_counter_reads,
        disk_lookup_keys=disk_lookup_keys,
    )
    return _select_sas_fabric_builder(context).build(context)


def _build_sas_fabric_result(
    *,
    available: bool,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    nodes: dict[str, SasFabricNode] | None = None,
    links: dict[str, SasFabricLink] | None = None,
    traces: dict[str, SasFabricTrace] | None = None,
    controllers: list[dict[str, Any]] | None = None,
    expanders: list[dict[str, Any]] | None = None,
    enclosures: list[dict[str, Any]] | None = None,
    paths: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
    warnings: list[str] | None = None,
    sources: dict[str, SourceStatus] | None = None,
    raw: dict[str, Any] | None = None,
    sort_controllers: bool = False,
) -> SasFabricSnapshot:
    # Keep bay IDs/kinds for selection and alias compatibility, but never present
    # a logical inventory ordinal as a known physical bay.
    location_unknown_slots = [slot for slot in snapshot.slots if not slot.physical_location_known]
    if location_unknown_slots:
        warnings = [*(warnings or []), (
            "Logical disk inventory (virtual): physical location unavailable for unmapped disks. "
            "Disk numbers are inventory ordinals, not physical bays."
        )]
        for slot in location_unknown_slots:
            object_id = f"bay:{slot.slot}"
            provenance = {
                "physical_location_known": False,
                "virtual_enclosure": bool(slot.raw_status.get("virtual_enclosure")),
            }
            node = (nodes or {}).get(object_id)
            trace = (traces or {}).get(object_id)
            for item in (node, trace):
                if item is not None:
                    item.label = f"Disk {slot.slot + 1}"
                    item.metrics.update(provenance)
                    item.evidence = _dedupe_strings([*item.evidence, "physical location unavailable"])
            if node is not None:
                node.raw.update(provenance)
        available = available or any(_slot_has_disk(slot) for slot in location_unknown_slots)
    controller_rows = controllers or []
    if sort_controllers:
        controller_rows = sorted(controller_rows, key=lambda item: str(item.get("name") or ""))
    return SasFabricSnapshot(
        available=available,
        system_id=system.id,
        system_label=system.label,
        platform=system.truenas.platform,
        selected_enclosure_id=snapshot.selected_enclosure_id,
        selected_enclosure_label=snapshot.selected_enclosure_label,
        nodes=sorted((nodes or {}).values(), key=lambda node: (node.kind, node.id)),
        links=sorted((links or {}).values(), key=lambda link: link.id),
        traces=sorted((traces or {}).values(), key=lambda trace: trace.id),
        controllers=controller_rows,
        expanders=sorted(expanders or [], key=lambda item: str(item.get("id") or "")),
        enclosures=sorted(enclosures or [], key=lambda item: str(item.get("id") or "")),
        paths=paths or [],
        aliases=aliases or [],
        warnings=warnings or [],
        sources=sources or {},
        raw=raw or {},
    )


def _build_unavailable_sas_fabric_snapshot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    fabric_kind: str,
    warnings: list[str],
    sources: dict[str, SourceStatus] | None,
    aliases: list[SasFabricAlias] | None,
    command_failures: list[dict[str, Any]] | None,
) -> SasFabricSnapshot:
    return _build_sas_fabric_result(
        available=False,
        system=system,
        snapshot=snapshot,
        aliases=list(aliases or []),
        warnings=warnings,
        sources=sources,
        raw={
            "fabric_domain": "storage_fabric",
            "fabric_kind": fabric_kind,
            "command_failures": list(command_failures or []),
        },
    )


def _add_host_node(
    nodes: dict[str, SasFabricNode],
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    metrics: dict[str, Any] | None = None,
    evidence: tuple[str, ...] = (),
    raw: dict[str, Any] | None = None,
) -> SasFabricNode:
    return add_node(
        nodes,
        SasFabricNode(
            id="host",
            kind="host",
            label=system.label or system.id,
            raw_id=system.id,
            metrics={"slot_count": len(snapshot.slots), **(metrics or {})},
            evidence=list(evidence),
            raw=raw or {},
        ),
    )


def _add_backplane_zone_nodes(
    nodes: dict[str, SasFabricNode],
    snapshot: InventorySnapshot,
) -> dict[int, dict[str, Any]]:
    backplane_zones = _backplane_zones_for_slots([slot for slot in snapshot.slots if slot.physical_location_known])
    for zone in backplane_zones.values():
        add_node(
            nodes,
            SasFabricNode(
                id=zone["id"],
                kind="backplane",
                label=zone["label"],
                related_slots=zone["slots"],
                metrics={
                    "zone": zone["index"] + 1,
                    "slot_range": zone["range"],
                    "slot_count": len(zone["slots"]),
                },
                evidence=list(_PROFILE_SLOT_LAYOUT_EVIDENCE),
                raw=zone,
            ),
        )
    return backplane_zones


def _add_pool_vdev_nodes_and_links(
    *,
    slot: SlotView,
    nodes: dict[str, SasFabricNode],
    links: dict[str, SasFabricLink],
    bay_id: str,
    bay_related_nodes: list[str],
    bay_related_links: list[str],
) -> None:
    related_slots = [slot.slot]
    if slot.pool_name:
        pool_id = _pool_node_id(slot.pool_name)
        add_node(nodes, SasFabricNode(id=pool_id, kind="pool", label=slot.pool_name, related_slots=related_slots))
        link = add_link(links, bay_id, pool_id, "bay-pool", slot=slot.slot, related_slots=related_slots)
        bay_related_nodes.append(pool_id)
        bay_related_links.append(link.id)
    if slot.vdev_name:
        vdev_id = _vdev_node_id(slot.vdev_name, slot.pool_name)
        add_node(nodes, SasFabricNode(id=vdev_id, kind="vdev", label=slot.vdev_name, related_slots=related_slots))
        target_id = _pool_node_id(slot.pool_name) if slot.pool_name else bay_id
        link = add_link(links, target_id, vdev_id, "pool-vdev", slot=slot.slot, related_slots=related_slots)
        bay_related_nodes.append(vdev_id)
        bay_related_links.append(link.id)


def _slot_linux_transport_detail(slot: SlotView) -> dict[str, Any]:
    return {
        "sg_device": getattr(slot, "sg_device", None),
        "scsi_hctl": getattr(slot, "scsi_hctl", None),
        "transport_protocol": getattr(slot, "transport_protocol", None),
        "attached_sas_address": getattr(slot, "attached_sas_address", None),
        "phy_identifier": getattr(slot, "phy_identifier", None),
        "target_port_protocol": getattr(slot, "target_port_protocol", None),
    }


def _build_core_mpr_fabric_snapshot(context: SasFabricBuildContext) -> SasFabricSnapshot:
    system = context.system
    snapshot = context.snapshot
    normalized_outputs = context.normalized_outputs
    sources = context.sources
    warnings = context.warnings
    command_failures = context.command_failures
    aliases = context.aliases
    alias_map = context.alias_map
    fabric_warnings = list(warnings or [])
    aliases_by_id = alias_map or _sas_fabric_alias_map(aliases or [])
    selected_enclosure_keys = _selected_enclosure_keys(snapshot)
    nodes: dict[str, SasFabricNode] = {}
    links: dict[str, SasFabricLink] = {}
    traces: dict[str, SasFabricTrace] = {}
    mpr_kernel_diagnostics = parse_mpr_dmesg_events(normalized_outputs.get("dmesg mpr events", ""))
    _add_host_node(
        nodes,
        system=system,
        snapshot=snapshot,
    )

    adapter_summary = parse_mpr_adapter_summary(normalized_outputs.get("mprutil show adapters", ""))
    unit_data = _parse_unit_data(normalized_outputs)
    pci_controllers = parse_pciconf_sas_controllers(normalized_outputs.get("pciconf -lv", ""))
    pcie_slots = parse_dmidecode_slots(normalized_outputs.get("dmidecode slot", ""))
    mpr_sysctl_locations = parse_mpr_sysctl_locations(normalized_outputs.get("mpr sysctl pci locations", ""))
    pci_inventory = _pci_inventory_by_controller(
        pci_controllers,
        pcie_slots,
        mpr_sysctl_locations,
    )
    cam_devices = parse_camcontrol_devlist(normalized_outputs.get("camcontrol devlist -v", ""))
    members_by_slot = _core_path_members(snapshot.slots, cam_devices, context.disk_lookup_keys)
    path_counts, path_slots = _path_counts_from_slots(snapshot.slots, members_by_slot)
    controller_names = sorted(
        # Only the summary's admitted units, inside the unit bound.
        {f"mpr{unit}" for unit in discover_mpr_units_from_adapter_summary(normalized_outputs.get("mprutil show adapters", ""))}
        | set(path_counts)
        | {_mpr_controller_name_for_unit(unit) for unit in unit_data}
    )

    controllers: list[dict[str, Any]] = []
    for controller_name in controller_names:
        # The unnumbered fallback has no observed unit; never derive one from its name.
        legacy_unit = controller_name == _LEGACY_MPR_CONTROLLER_NAME and _LEGACY_MPR_UNIT_KEY in unit_data
        unit = None if legacy_unit else _mpr_unit_from_text(controller_name)
        summary_row = next((row for row in adapter_summary if row.get("unit") == unit), None) if unit is not None else None
        if legacy_unit:
            parsed_unit = unit_data[_LEGACY_MPR_UNIT_KEY]
        else:
            parsed_unit = unit_data.get(int(unit)) if unit is not None and unit.isdigit() else None
        detail = parsed_unit.get("adapter_detail", {}) if parsed_unit else {}
        iocfacts = parsed_unit.get("iocfacts", {}) if parsed_unit else {}
        pci_info = pci_inventory.get(controller_name, {})
        pcie_slot = pci_info.get("pcie_slot")
        controller_diagnostics = mpr_kernel_diagnostics.get("by_controller", {}).get(controller_name, {})
        counts = dict(path_counts.get(controller_name, {}))
        related_slots = _dedupe_ints(slot for slots in path_slots.get(controller_name, {}).values() for slot in slots)
        if unit is not None:
            controller_evidence = [f"mprutil -u {unit} show adapter"]
        elif legacy_unit:
            controller_evidence = _mpr_evidence(_LEGACY_MPR_UNIT_KEY, "adapter")
        else:
            controller_evidence = ["mprutil show adapters"]
        if pci_info.get("pci_address"):
            controller_evidence.append("pciconf -lv")
        if pci_info.get("sysctl_pci_location"):
            controller_evidence.append("sysctl dev.mpr.%location")
        if pcie_slot:
            controller_evidence.append("dmidecode -t slot")
        controller = {
            "id": f"controller:{controller_name}",
            "name": controller_name,
            "unit": int(unit) if unit is not None and unit.isdigit() else None,
            "device": summary_row.get("device") if summary_row else (None if legacy_unit else f"/dev/{controller_name}"),
            "board": detail.get("board_name") or (summary_row or {}).get("board"),
            "chip": (summary_row or {}).get("chip"),
            "firmware": detail.get("firmware_revision") or (summary_row or {}).get("firmware"),
            "temperature": detail.get("temperature"),
            "pci_address": pci_info.get("pci_address"),
            "pci_location": pci_info.get("pci_location"),
            "pci_parent": pci_info.get("pci_parent"),
            "acpi_handle": pci_info.get("acpi_handle"),
            "pcie_slot": pcie_slot,
            "pcie_slot_type": pci_info.get("pcie_slot_type"),
            "pcie_slot_usage": pci_info.get("pcie_slot_usage"),
            "pci_vendor": pci_info.get("vendor_name"),
            "pci_device": pci_info.get("device_name"),
            "pci": pci_info or None,
            "path_counts": counts,
            "related_slots": related_slots,
            "iocfacts": iocfacts,
            # The parsed adapter PHY table, kept as reported (#937). None when
            # no unit data exists for this controller.
            "phy_rows": [dict(row) for row in detail.get("phy_rows") or []] if parsed_unit else None,
        }
        controllers.append(controller)
        add_node(
            nodes,
            SasFabricNode(
                id=controller["id"],
                kind="controller",
                label=controller_name,
                raw_id=controller.get("device"),
                status=_controller_status(counts),
                related_slots=related_slots,
                metrics={
                    "path_counts": counts,
                    "firmware": controller.get("firmware"),
                    "temperature": controller.get("temperature"),
                    "pcie_slot": controller.get("pcie_slot"),
                    "pci_address": controller.get("pci_address"),
                    "phy_count": detail.get("phy_count"),
                    "linked_phy_count": detail.get("linked_phy_count"),
                    "kernel_diagnostics": _diagnostic_metric_summary(controller_diagnostics),
                    # A PHY table that was seen but not read whole: "not read", not absent.
                    **({"not_read": ["phy_count", "linked_phy_count"]} if detail.get("phy_table") == "rejected" else {}),
                },
                evidence=_dedupe_strings(controller_evidence),
                raw={key: value for key, value in controller.items() if key not in {"id", "related_slots"}},
            ),
        )
        add_link(links, "host", controller["id"], "host-controller", related_slots=related_slots)

    paths: list[dict[str, Any]] = []
    slots_by_number = {slot.slot: slot for slot in snapshot.slots}
    for controller_name, state_counts in sorted(path_counts.items()):
        controller_id = f"controller:{controller_name}"
        for state, count in sorted(state_counts.items(), key=lambda item: (_path_state_sort_key(item[0]), item[0])):
            slots = sorted(path_slots[controller_name][state])
            path_id = f"path:{controller_name}:{state}"
            path_evidence = _dedupe_strings(
                [evidence for number in slots for evidence in _slot_path_evidence(slots_by_number[number])]
            )
            paths.append(
                {
                    "id": path_id,
                    "controller": controller_name,
                    "state": state,
                    "count": count,
                    "slots": slots,
                }
            )
            add_node(
                nodes,
                SasFabricNode(
                    id=path_id,
                    kind="path",
                    label=f"{controller_name} {state}",
                    status=state,
                    controller_id=controller_id,
                    related_slots=slots,
                    metrics={"count": count},
                    evidence=path_evidence,
                ),
            )
            add_link(links, controller_id, path_id, "controller-path", status=state, related_slots=slots)
            traces[path_id] = SasFabricTrace(
                id=path_id,
                label=f"{controller_name} {state}",
                kind="path",
                node_ids=["host", controller_id, path_id],
                link_ids=[_link_id("host", controller_id, "host-controller"), _link_id(controller_id, path_id, "controller-path")],
                slots=slots,
                metrics={"count": count, "state": state},
                evidence=path_evidence,
            )

    ses_devices_by_slot = _ses_devices_by_slot(snapshot.slots)
    _add_mpr_infrastructure(
        nodes=nodes,
        links=links,
        controllers=controllers,
        unit_data=unit_data,
        selected_enclosure_keys=selected_enclosure_keys,
    )
    mpr_trace_index = _build_mpr_trace_index(
        unit_data=unit_data,
        controllers=controllers,
        diagnostics=mpr_kernel_diagnostics,
    )
    # CAM's per-device target on its own controller is the identity that ties a
    # member disk to its `mprutil show devices` row (#936).
    backplane_zones = _add_backplane_zone_nodes(nodes, snapshot)

    for slot in snapshot.slots:
        bay_id = f"bay:{slot.slot}"
        bay_related_nodes = ["host", bay_id]
        bay_related_links: list[str] = []
        mpr_device_metrics: list[dict[str, Any]] = []
        backplane_zone = backplane_zones.get(slot.slot)
        add_node(
            nodes,
            SasFabricNode(
                id=bay_id,
                kind="bay",
                label=f"Bay {_bay_label(slot)}",
                slot=slot.slot,
                status=slot.state.value if hasattr(slot.state, "value") else str(slot.state),
                related_slots=[slot.slot],
                metrics={
                    "present": _bay_present_metric(slot),
                    "pool_name": slot.pool_name,
                    "vdev_name": slot.vdev_name,
                    "device_name": slot.device_name,
                    "health": slot.health,
                },
                evidence=list(_INVENTORY_SNAPSHOT_EVIDENCE),
            ),
        )
        if backplane_zone:
            backplane_link = add_link(
                links,
                backplane_zone["id"],
                bay_id,
                "backplane-bay",
                slot=slot.slot,
                related_slots=[slot.slot],
                evidence=list(_PROFILE_SLOT_LAYOUT_EVIDENCE),
            )
            bay_related_nodes.append(backplane_zone["id"])
            bay_related_links.append(backplane_link.id)
        slot_members = members_by_slot[slot.slot]
        direct_members = not (slot.multipath and slot.multipath.members)
        for member in slot_members:
            controller_name = normalize_text(member.controller_label)
            if not controller_name:
                continue
            state = normalize_path_state(member.state)
            controller_id = f"controller:{controller_name}"
            path_id = f"path:{controller_name}:{state}"
            link = add_link(links, path_id, bay_id, "path-bay", status=state, slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.extend([controller_id, path_id])
            bay_related_links.extend(
                [
                    _link_id("host", controller_id, "host-controller"),
                    _link_id(controller_id, path_id, "controller-path"),
                ]
            )
            bay_related_links.append(link.id)
            # Direct paths require CAM identity. Do not use the legacy location
            # fallback when their controller-local target was not read.
            if direct_members and _cam_member_target(cam_devices, controller_name, member.device_name) is None:
                continue
            mpr_context = _add_mpr_member_trace_context(
                nodes=nodes,
                links=links,
                trace_index=mpr_trace_index,
                controller_name=controller_name,
                path_id=path_id,
                bay_id=bay_id,
                slot_number=slot.slot,
                path_state=state,
                member_device_name=member.device_name,
                member_target=_cam_member_target(cam_devices, controller_name, member.device_name),
                member_target_rejected=_cam_member_target_rejected(cam_devices, controller_name, member.device_name),
                slot_enclosure_ids=_slot_enclosure_candidates(slot),
                slot_location_numbers=_slot_location_number_candidates(slot),
                slot_ses_number=_parse_mpr_slot_number((slot.raw_status or {}).get("ses_slot_number")),
                diagnostics=mpr_kernel_diagnostics,
            )
            if mpr_context:
                bay_related_nodes.extend(mpr_context["node_ids"])
                bay_related_links.extend(mpr_context["bay_link_ids"])
                mpr_device_metrics.append(mpr_context["metric"])
                path_trace = traces.get(path_id)
                if path_trace:
                    path_trace.node_ids = _dedupe_strings([*path_trace.node_ids, *mpr_context["node_ids"]])
                    path_trace.link_ids = _dedupe_strings([*path_trace.link_ids, *mpr_context["path_link_ids"]])
                    path_trace.evidence = _dedupe_strings([*path_trace.evidence, *mpr_context["evidence"]])
                    path_trace.metrics["mpr_device_count"] = int(path_trace.metrics.get("mpr_device_count") or 0) + 1
                    path_trace.metrics["mpr_enclosure_count"] = len(
                        {
                            node_id
                            for node_id in path_trace.node_ids
                            if node_id.startswith("mpr-enclosure:")
                        }
                    )
                    path_trace.metrics["expander_count"] = len(
                        {
                            node_id
                            for node_id in path_trace.node_ids
                            if node_id.startswith("expander:")
                        }
                    )
        for ses_device in ses_devices_by_slot.get(slot.slot, []):
            ses_id = f"ses:{ses_device}"
            add_node(
                nodes,
                SasFabricNode(
                    id=ses_id,
                    kind="ses-enclosure",
                    label=ses_device,
                    raw_id=ses_device,
                    related_slots=[slot.slot],
                    evidence=["SES enclosure device"],
                    raw={"ses_device": ses_device},
                ),
            )
            link = add_link(links, ses_id, bay_id, "ses-bay", slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.append(ses_id)
            bay_related_links.append(link.id)
        _add_pool_vdev_nodes_and_links(
            slot=slot,
            nodes=nodes,
            links=links,
            bay_id=bay_id,
            bay_related_nodes=bay_related_nodes,
            bay_related_links=bay_related_links,
        )
        traces[bay_id] = SasFabricTrace(
            id=bay_id,
            label=f"Bay {_bay_label(slot)}",
            kind="bay",
            node_ids=_dedupe_strings(bay_related_nodes),
            link_ids=_dedupe_strings(bay_related_links),
            slots=[slot.slot],
            metrics={
                "device_name": slot.device_name,
                "pool_name": slot.pool_name,
                "vdev_name": slot.vdev_name,
                "path_states": [
                    {
                        "controller": member.controller_label,
                        "state": normalize_path_state(member.state),
                        "device_name": member.device_name,
                        **(
                            _direct_member_not_read(member, cam_devices) if direct_members
                            else _multipath_member_not_read(member, observed)
                        ),
                    }
                    for member, observed in zip(
                        slot_members, slot_members if direct_members else slot.multipath.members, strict=True,
                    )
                ],
                "mpr_devices": mpr_device_metrics,
            },
            evidence=["inventory snapshot", *(_slot_path_evidence(slot) if slot_members else _SLOT_MULTIPATH_EVIDENCE)],
        )

    fabric_warnings.extend(_scope_mpr_infrastructure(
        nodes=nodes,
        links=links,
        traces=traces,
        snapshot=snapshot,
    ))
    _attach_expander_phy_error_counters(
        nodes, normalized_outputs, context.expander_counter_capability, context.expander_counter_reads,
    )
    for controller in controllers:
        controller_node = nodes.get(str(controller.get("id") or ""))
        if controller_node:
            controller["related_slots"] = list(controller_node.related_slots)

    aliases_by_id = _resolve_sas_fabric_alias_map(aliases_by_id, nodes)
    _apply_sas_fabric_aliases(
        nodes=nodes,
        traces=traces,
        controllers=controllers,
        paths=paths,
        aliases=aliases_by_id,
    )

    expanders = [_node_raw_for_payload(node) for node in nodes.values() if node.kind == "expander"]
    enclosures = [
        _node_raw_for_payload(node)
        for node in nodes.values()
        if node.kind in {"mpr-enclosure", "ses-enclosure"}
    ]

    unit_reads, rejected_units = _mpr_unit_read_results(normalized_outputs, unit_data, command_failures)
    adapter_summary_text = normalized_outputs.get("mprutil show adapters", "")
    rejected_summary_rows = rejected_mpr_summary_rows(adapter_summary_text)
    summary_units_over_limit = mpr_summary_units_over_limit(adapter_summary_text)
    unread_units = [item["controller"] for item in unit_reads if item["result"] != "read"]
    if not unit_data and any(item["result"] == "unreadable" for item in unit_reads):
        fabric_warnings.append(
            "CORE mprutil output was returned but could not be read as controller data, "
            "so controller details are unknown."
        )
    elif not controller_names and not paths:
        fabric_warnings.append("No CORE SAS controller/path data is available yet. Check SSH and mprutil permissions.")
    elif not unit_data:
        fabric_warnings.append(
            "CORE mprutil returned no controller data, so expander and enclosure counts and "
            "controller details were not read."
        )
    elif unread_units:
        fabric_warnings.append(
            f"CORE controller data could not be read for {_bounded_name_list(unread_units)}, "
            "so controller, expander and enclosure counts may be incomplete."
        )
    else:
        missing_lists = _dedupe_strings([
            name
            for subcommand in ("expanders", "enclosures")
            for name in _mpr_units_missing_list(unit_data, subcommand)
        ])
        if missing_lists:
            fabric_warnings.append(
                f"CORE controller {_bounded_name_list(missing_lists)} did not return a readable expander or "
                "enclosure list, so those counts may be incomplete."
            )
    if rejected_units:
        fabric_warnings.append(
            f"CORE mprutil listed {rejected_units} controller "
            + ("unit that is not a unit number, so it was" if rejected_units == 1 else "units that are not unit numbers, so they were")
            + " not read."
        )
    if rejected_summary_rows:
        fabric_warnings.append(
            f"The CORE mprutil adapter list had {rejected_summary_rows} "
            + ("row that is not an adapter device name, so it was" if rejected_summary_rows == 1 else "rows that are not an adapter device name, so they were")
            + " not read and controller counts may be incomplete."
        )
    if summary_units_over_limit:
        fabric_warnings.append(
            f"The CORE mprutil adapter list named more than {MAX_CORE_MPR_SUMMARY_UNITS} controller units; "
            f"{summary_units_over_limit} {'was' if summary_units_over_limit == 1 else 'were'} not read, "
            "so controller, expander and enclosure counts may be incomplete."
        )
    unread_tables = Counter(
        node.kind
        for node in nodes.values()
        if node.kind in {"controller", "expander"} and {"phy_count", "linked_phys"} & set(node.metrics.get("not_read") or [])
    )
    if unread_tables:
        phrase = " and ".join(
            f"{unread_tables[kind]} {kind}{'' if unread_tables[kind] == 1 else 's'}"
            for kind in ("controller", "expander")
            if unread_tables[kind]
        )
        fabric_warnings.append(f"CORE PHY tables could not be read whole for {phrase}, so their linked PHY counts were not read.")
    controller_read = {
        "units": unit_reads,
        "counts": _mpr_count_completeness(
            unit_data,
            unit_reads,
            # Summary rows and units that were not read leave the counts short too.
            rejected_units=rejected_units + rejected_summary_rows + summary_units_over_limit,
            controllers_seen=bool(controller_names),
            paths_seen=bool(paths),
        ),
        # Per read unit: whether each list reply was read, printed empty (a
        # measured zero), unreadable, or not kept at all.
        "lists": {_mpr_controller_name_for_unit(unit): dict(payload["lists"]) for unit, payload in sorted(unit_data.items())},
        "rejected_units": rejected_units,
        # Adapter summary rows that name no adapter device, and admitted units
        # past the summary's unit bound: counts only, never the text.
        "rejected_summary_rows": rejected_summary_rows,
        "summary_units_over_limit": summary_units_over_limit,
        "unread_phy_tables": sum(unread_tables.values()),
    }
    # Without controller data the bays, pools and vdevs the inventory already
    # knows are still drawn; the warning above says what could not be read.
    inventory_drawn = any(node.kind in {"bay", "pool", "vdev"} for node in nodes.values())

    return _build_sas_fabric_result(
        available=bool(controller_names or paths or expanders or enclosures or inventory_drawn),
        system=system,
        snapshot=snapshot,
        nodes=nodes,
        links=links,
        traces=traces,
        controllers=controllers,
        expanders=expanders,
        enclosures=enclosures,
        paths=paths,
        aliases=list(aliases_by_id.values()),
        warnings=fabric_warnings,
        sources=sources,
        raw={
            "commands": sorted(normalized_outputs),
            "path_counts": {controller: dict(counts) for controller, counts in sorted(path_counts.items())},
            "path_slots": {
                controller: {state: sorted(slots) for state, slots in sorted(states.items())}
                for controller, states in sorted(path_slots.items())
            },
            "selected_enclosure_keys": sorted(selected_enclosure_keys),
            "selected_bay_slots": _snapshot_slot_numbers(snapshot.slots),
            "selected_disk_slots": _snapshot_disk_slot_numbers(snapshot.slots),
            "pci_controllers": pci_controllers,
            "pcie_slots": pcie_slots,
            "mpr_sysctl_locations": mpr_sysctl_locations,
            "mpr_kernel_events": {
                "event_count": mpr_kernel_diagnostics.get("event_count", 0),
                "controllers": sorted((mpr_kernel_diagnostics.get("by_controller") or {}).keys()),
                "devices": sorted((mpr_kernel_diagnostics.get("by_device") or {}).keys()),
            },
            "command_failures": list(command_failures or []),
            # Which controller-side counts were measured: the page shows
            # "not read" instead of 0 for a count whose read did not happen.
            "controller_read": controller_read,
        },
    )


def _slot_storage_identity(slot: SlotView) -> dict[str, Any]:
    values: dict[str, Any] = {
        "serial": slot.serial,
        "model": slot.model,
        "size_bytes": slot.size_bytes,
        "size_human": slot.size_human,
        "gptid": slot.gptid,
        "persistent_id_label": slot.persistent_id_label,
        "vdev_class": slot.vdev_class,
        "topology_label": slot.topology_label,
        "logical_block_size": slot.logical_block_size,
        "physical_block_size": slot.physical_block_size,
        "logical_unit_id": slot.logical_unit_id,
        "sas_address": slot.sas_address,
        "enclosure_id": slot.enclosure_id,
        "enclosure_label": slot.enclosure_label,
        "enclosure_name": slot.enclosure_name,
        "enclosure_identifier": slot.enclosure_identifier,
        "smart_device_type": slot.smart_device_type,
    }
    if slot.smart_device_names:
        values["smart_device_names"] = list(slot.smart_device_names)
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    for key in (
        "sas_device_type",
        "transport_address",
        "linux_hctl",
        "linux_transport",
        "ses_slot_number",
        "ses_disabled",
        "ses_do_not_remove",
        "ses_fault_requested",
        "ses_fault_sensed",
        "ses_predicted_failure",
        "ses_swapped",
    ):
        if key in raw_status:
            values[key] = raw_status.get(key)
    return {
        key: value
        for key, value in values.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _build_linux_ses_fabric_snapshot(context: SasFabricBuildContext) -> SasFabricSnapshot:
    system = context.system
    snapshot = context.snapshot
    sources = context.sources
    warnings = context.warnings
    command_failures = context.command_failures
    aliases = context.aliases
    alias_map = context.alias_map
    fabric_warnings = list(warnings or [])
    aliases_by_id = alias_map or _sas_fabric_alias_map(aliases or [])
    slots_with_ses = [
        slot
        for slot in snapshot.slots
        if _slot_ses_devices(slot)
    ]
    platform_label = "TrueNAS SCALE" if system.truenas.platform == "scale" else "Linux"
    if not slots_with_ses:
        fabric_warnings.append(
            f"No SES enclosure data was found for this {platform_label} system. "
            "Check that SSH works and that sg_ses is installed on the host (see Troubleshooting)."
        )
        return _build_unavailable_sas_fabric_snapshot(
            system=system,
            snapshot=snapshot,
            fabric_kind="linux_ses",
            warnings=fabric_warnings,
            sources=sources,
            aliases=aliases,
            command_failures=command_failures,
        )

    nodes: dict[str, SasFabricNode] = {}
    links: dict[str, SasFabricLink] = {}
    traces: dict[str, SasFabricTrace] = {}
    selected_slots = _snapshot_slot_numbers(snapshot.slots)
    selected_disk_slots = _snapshot_disk_slot_numbers(snapshot.slots)
    ses_slots: dict[str, list[SlotView]] = defaultdict(list)
    for slot in slots_with_ses:
        primary_ses = _slot_primary_ses_device(slot)
        if primary_ses:
            ses_slots[primary_ses].append(slot)

    controller_id = "controller:linux-ses"
    controller_name = "linux-ses"
    ses_device_count = len(ses_slots)
    _add_host_node(
        nodes,
        system=system,
        snapshot=snapshot,
        metrics={
            "selected_disk_count": len(selected_disk_slots),
            "fabric_domain": "storage_fabric",
            "fabric_kind": "linux_ses",
        },
        evidence=_INVENTORY_SNAPSHOT_EVIDENCE,
        raw={"platform": system.truenas.platform, "fabric_domain": "storage_fabric", "fabric_kind": "linux_ses"},
    )
    add_node(
        nodes,
        SasFabricNode(
            id=controller_id,
            kind="controller",
            label="Linux SES",
            raw_id="lsscsi -g -t / sg_ses",
            status="online",
            related_slots=selected_slots,
            metrics={
                "path_counts": {"mapped": len(slots_with_ses)},
                "ses_device_count": ses_device_count,
                "selected_bay_count": len(selected_slots),
                "selected_disk_count": len(selected_disk_slots),
                "fabric_kind": "linux_ses",
            },
            evidence=list(_LINUX_SES_PATH_EVIDENCE),
            raw={
                "device": "lsscsi -g -t",
                "board": f"{platform_label} Linux SES",
                "path_counts": {"mapped": len(slots_with_ses)},
                "source": "linux_ses",
                "fabric_domain": "storage_fabric",
                "fabric_kind": "linux_ses",
            },
        ),
    )
    add_link(
        links,
        "host",
        controller_id,
        "host-controller",
        related_slots=selected_slots,
        evidence=list(_INVENTORY_SNAPSHOT_EVIDENCE),
    )

    paths: list[dict[str, Any]] = []
    enclosures_by_ses: dict[str, str] = {}
    for ses_device, slots in sorted(ses_slots.items(), key=lambda item: _linux_ses_sort_key(item[0])):
        ses_token = _object_id_token(ses_device)
        ses_slot_numbers = _dedupe_ints(slot.slot for slot in slots)
        sample_slot = slots[0]
        path_id = f"path:{controller_name}:{ses_token}"
        ses_id = f"ses:{ses_token}"
        enclosures_by_ses[ses_device] = ses_id
        path = {
            "id": path_id,
            "controller": controller_name,
            "state": "mapped",
            "count": len(ses_slot_numbers),
            "slots": ses_slot_numbers,
            "label": f"{ses_device} mapped",
            "ses_device": ses_device,
            "source": "linux_ses",
            "fabric_kind": "linux_ses",
        }
        paths.append(path)
        add_node(
            nodes,
            SasFabricNode(
                id=path_id,
                kind="path",
                label=f"{ses_device} mapped",
                status="mapped",
                controller_id=controller_id,
                related_slots=ses_slot_numbers,
                metrics={"count": len(ses_slot_numbers), "ses_device": ses_device},
                evidence=list(_LINUX_SES_PATH_EVIDENCE),
                raw={"ses_device": ses_device, "source": "linux_ses", "fabric_domain": "storage_fabric", "fabric_kind": "linux_ses"},
            ),
        )
        add_node(
            nodes,
            SasFabricNode(
                id=ses_id,
                kind="ses-enclosure",
                label=snapshot.selected_enclosure_label or sample_slot.enclosure_label or ses_device,
                raw_id=ses_device,
                controller_id=controller_id,
                related_slots=ses_slot_numbers,
                metrics={
                    "slot_count": len(ses_slot_numbers),
                    "ses_device": ses_device,
                    "selected_enclosure": True,
                    "selected_disk_count": len([slot for slot in slots if _slot_has_disk(slot)]),
                },
                evidence=list(_LINUX_SES_ENCLOSURE_EVIDENCE),
                raw={
                    "id": ses_id,
                    "ses_device": ses_device,
                    "enclosure_id": sample_slot.enclosure_id or snapshot.selected_enclosure_id,
                    "enclosure_label": sample_slot.enclosure_label or snapshot.selected_enclosure_label,
                    "enclosure_name": sample_slot.enclosure_name or snapshot.selected_enclosure_name,
                    "source": "linux_ses",
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "linux_ses",
                },
            ),
        )
        add_link(
            links,
            controller_id,
            path_id,
            "controller-path",
            status="mapped",
            related_slots=ses_slot_numbers,
            evidence=["lsscsi -g", "lsscsi -g -t"],
        )
        add_link(
            links,
            path_id,
            ses_id,
            "path-ses-enclosure",
            status="mapped",
            related_slots=ses_slot_numbers,
            evidence=list(_LINUX_SES_ENCLOSURE_EVIDENCE),
        )
        traces[path_id] = SasFabricTrace(
            id=path_id,
            label=f"{ses_device} mapped",
            kind="path",
            node_ids=["host", controller_id, path_id, ses_id],
            link_ids=[
                _link_id("host", controller_id, "host-controller"),
                _link_id(controller_id, path_id, "controller-path"),
                _link_id(path_id, ses_id, "path-ses-enclosure"),
            ],
            slots=ses_slot_numbers,
            metrics={"count": len(ses_slot_numbers), "state": "mapped", "ses_device": ses_device},
            evidence=list(_LINUX_SES_PATH_EVIDENCE),
        )

    backplane_zones = _add_backplane_zone_nodes(nodes, snapshot)

    for slot in snapshot.slots:
        slot_identity = _slot_storage_identity(slot)
        transport_detail = _slot_linux_transport_detail(slot)
        bay_id = f"bay:{slot.slot}"
        ses_device = _slot_primary_ses_device(slot)
        ses_id = enclosures_by_ses.get(ses_device or "")
        path_id = f"path:{controller_name}:{_object_id_token(ses_device)}" if ses_device else None
        bay_related_nodes = _dedupe_strings(["host", controller_id, *( [path_id] if path_id else [] ), *( [ses_id] if ses_id else [] ), bay_id])
        bay_related_links = [
            _link_id("host", controller_id, "host-controller"),
            *([_link_id(controller_id, path_id, "controller-path")] if path_id else []),
            *([_link_id(path_id, ses_id, "path-ses-enclosure")] if path_id and ses_id else []),
        ]
        backplane_zone = backplane_zones.get(slot.slot)
        add_node(
            nodes,
            SasFabricNode(
                id=bay_id,
                kind="bay",
                label=f"Bay {_bay_label(slot)}",
                slot=slot.slot,
                status=slot.state.value if hasattr(slot.state, "value") else str(slot.state),
                related_slots=[slot.slot],
                metrics={
                    "present": _bay_present_metric(slot),
                    "pool_name": slot.pool_name,
                    "vdev_name": slot.vdev_name,
                    "device_name": slot.device_name,
                    "health": slot.health,
                    "sas_address": slot.sas_address,
                    **transport_detail,
                    "ses_device": ses_device,
                    "ses_element_id": slot.ssh_ses_element_id,
                    **slot_identity,
                },
                evidence=list(_LINUX_SES_BAY_EVIDENCE),
                raw={
                    **slot_identity,
                    "ses_device": ses_device,
                    "ses_element_id": slot.ssh_ses_element_id,
                    "ses_targets": list(slot.ssh_ses_targets),
                    **transport_detail,
                    "linux_blockdevice": slot.raw_status.get("linux_blockdevice") if isinstance(slot.raw_status, dict) else None,
                    "linux_scsi_device": slot.raw_status.get("linux_scsi_device") if isinstance(slot.raw_status, dict) else None,
                    "mapping_source": slot.mapping_source,
                    "source": "linux_ses",
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "linux_ses",
                },
            ),
        )
        if backplane_zone:
            backplane_link = add_link(
                links,
                backplane_zone["id"],
                bay_id,
                "backplane-bay",
                slot=slot.slot,
                related_slots=[slot.slot],
                evidence=list(_PROFILE_SLOT_LAYOUT_EVIDENCE),
            )
            bay_related_nodes.append(backplane_zone["id"])
            bay_related_links.append(backplane_link.id)
        if ses_id:
            ses_link = add_link(
                links,
                ses_id,
                bay_id,
                "ses-bay",
                slot=slot.slot,
                related_slots=[slot.slot],
                evidence=list(_SG_SES_AES_EVIDENCE),
            )
            bay_related_links.append(ses_link.id)
        _add_pool_vdev_nodes_and_links(
            slot=slot,
            nodes=nodes,
            links=links,
            bay_id=bay_id,
            bay_related_nodes=bay_related_nodes,
            bay_related_links=bay_related_links,
        )
        traces[bay_id] = SasFabricTrace(
            id=bay_id,
            label=f"Bay {_bay_label(slot)}",
            kind="bay",
            node_ids=_dedupe_strings(bay_related_nodes),
            link_ids=_dedupe_strings(bay_related_links),
            slots=[slot.slot],
            metrics={
                "device_name": slot.device_name,
                "pool_name": slot.pool_name,
                "vdev_name": slot.vdev_name,
                "fabric_kind": "linux_ses",
                **slot_identity,
                "path_states": [
                    {
                        "controller": controller_name,
                        "state": "mapped" if ses_device else "unmapped",
                        "device_name": slot.device_name,
                        "pool_name": slot.pool_name,
                        "vdev_name": slot.vdev_name,
                        "ses_device": ses_device,
                        **transport_detail,
                        "path_id": path_id,
                        **slot_identity,
                    }
                ],
                "mpr_devices": [],
                "ses_device": ses_device,
                "ses_element_id": slot.ssh_ses_element_id,
            },
            evidence=list(_LINUX_SES_BAY_EVIDENCE),
        )

    controllers = [
        {
            "id": controller_id,
            "name": controller_name,
            "device": "lsscsi -g -t",
            "board": f"{platform_label} Linux SES",
            "path_counts": {"mapped": len(slots_with_ses)},
            "related_slots": selected_slots,
            "source": "linux_ses",
            "fabric_kind": "linux_ses",
        }
    ]
    aliases_by_id = _resolve_sas_fabric_alias_map(aliases_by_id, nodes)
    _apply_sas_fabric_aliases(
        nodes=nodes,
        traces=traces,
        controllers=controllers,
        paths=paths,
        aliases=aliases_by_id,
    )
    enclosures = [
        _node_raw_for_payload(node)
        for node in nodes.values()
        if node.kind == "ses-enclosure"
    ]
    fabric_warnings.append(
        f"{platform_label} Storage Fabric map is built from Linux block, SCSI transport, and SES slot evidence. "
        "HBA and expander hop detail is not exposed by this Linux SES evidence."
    )
    return _build_sas_fabric_result(
        available=True,
        system=system,
        snapshot=snapshot,
        nodes=nodes,
        links=links,
        traces=traces,
        controllers=controllers,
        expanders=[],
        enclosures=enclosures,
        paths=paths,
        aliases=list(aliases_by_id.values()),
        warnings=fabric_warnings,
        sources=sources,
        raw={
            "fabric_domain": "storage_fabric",
            "fabric_kind": "linux_ses",
            "ses_devices": sorted(ses_slots),
            "selected_bay_slots": selected_slots,
            "selected_disk_slots": selected_disk_slots,
            "command_failures": list(command_failures or []),
        },
    )


def _build_platform_storage_fabric_snapshot(context: SasFabricBuildContext) -> SasFabricSnapshot:
    system = context.system
    snapshot = context.snapshot
    sources = context.sources
    warnings = context.warnings
    command_failures = context.command_failures
    aliases = context.aliases
    alias_map = context.alias_map
    fabric_warnings = list(warnings or [])
    aliases_by_id = alias_map or _sas_fabric_alias_map(aliases or [])
    platform = normalize_text(system.truenas.platform).lower()
    platform_label = _platform_label(platform)
    fabric_kind = _storage_fabric_kind(platform)
    evidence_slots = [slot for slot in snapshot.slots if _slot_has_storage_fabric_evidence(slot)]

    if not evidence_slots:
        fabric_warnings.append(
            f"No disk or enclosure data was found for {platform_label}, so there is nothing to map yet."
        )
        return _build_unavailable_sas_fabric_snapshot(
            system=system,
            snapshot=snapshot,
            fabric_kind=fabric_kind,
            warnings=fabric_warnings,
            sources=sources,
            aliases=aliases,
            command_failures=command_failures,
        )

    nodes: dict[str, SasFabricNode] = {}
    links: dict[str, SasFabricLink] = {}
    traces: dict[str, SasFabricTrace] = {}
    selected_slots = _snapshot_slot_numbers(snapshot.slots)
    selected_disk_slots = _snapshot_disk_slot_numbers(snapshot.slots)
    _add_host_node(
        nodes,
        system=system,
        snapshot=snapshot,
        metrics={
            "selected_disk_count": len(selected_disk_slots),
            "fabric_domain": "storage_fabric",
            "fabric_kind": fabric_kind,
        },
        evidence=_INVENTORY_SNAPSHOT_EVIDENCE,
        raw={"platform": system.truenas.platform, "fabric_kind": fabric_kind},
    )

    controllers_by_name: dict[str, dict[str, Any]] = {}
    paths_by_id: dict[str, dict[str, Any]] = {}
    enclosure_nodes: dict[str, SasFabricNode] = {}
    backplane_zones = _add_backplane_zone_nodes(nodes, snapshot)

    for slot in evidence_slots:
        raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
        route = _storage_fabric_route_for_slot(
            system=system,
            snapshot=snapshot,
            slot=slot,
            fabric_kind=fabric_kind,
            platform_label=platform_label,
        )
        controller_id = route["controller_id"]
        controller_name = route["controller_name"]
        path_id = route["path_id"]
        enclosure_id = route["enclosure_id"]
        slot_numbers = [slot.slot]
        controller_slots = controllers_by_name.setdefault(
            controller_id,
            {
                "id": controller_id,
                "name": controller_name,
                "label": route["controller_label"],
                "device": route.get("controller_device"),
                "board": route.get("controller_label"),
                "path_counts": Counter(),
                "related_slots": [],
                "source": route.get("source"),
                "fabric_kind": fabric_kind,
            },
        )
        controller_slots["path_counts"][route["path_state"]] += 1
        controller_slots["related_slots"] = _dedupe_ints([*controller_slots["related_slots"], slot.slot])
        add_node(
            nodes,
            SasFabricNode(
                id=controller_id,
                kind="controller",
                label=route["controller_label"],
                raw_id=route.get("controller_device"),
                status=route["controller_status"],
                related_slots=slot_numbers,
                metrics={
                    "path_counts": dict(controller_slots["path_counts"]),
                    "fabric_kind": fabric_kind,
                    "source": route.get("source"),
                },
                evidence=route["controller_evidence"],
                raw={
                    "device": route.get("controller_device"),
                    "board": route["controller_label"],
                    "source": route.get("source"),
                    "fabric_kind": fabric_kind,
                    **route.get("controller_raw", {}),
                },
            ),
        )
        add_link(
            links,
            "host",
            controller_id,
            "host-controller",
            related_slots=slot_numbers,
            evidence=list(_INVENTORY_SNAPSHOT_EVIDENCE),
        )

        path = paths_by_id.setdefault(
            path_id,
            {
                "id": path_id,
                "controller": controller_name,
                "controller_label": route["controller_label"],
                "controller_id": controller_id,
                "state": route["path_state"],
                "member_state_counts": Counter(),
                "state_basis": "member-health-not-transport",
                "count": 0,
                "slots": [],
                "label": route["path_label"],
                "source": route.get("source"),
                "fabric_kind": fabric_kind,
                "path_type": route.get("path_type"),
            },
        )
        path["count"] += 1
        path["member_state_counts"][route["path_state"]] += 1
        path["slots"] = _dedupe_ints([*path["slots"], slot.slot])
        add_node(
            nodes,
            SasFabricNode(
                id=path_id,
                kind="path",
                label=route["path_label"],
                status=route["path_state"],
                controller_id=controller_id,
                related_slots=slot_numbers,
                metrics={
                    "count": path["count"],
                    "path_type": route.get("path_type"),
                    "fabric_kind": fabric_kind,
                },
                evidence=route["path_evidence"],
                raw=route["path_raw"],
            ),
        )
        add_link(
            links,
            controller_id,
            path_id,
            "controller-path",
            status=route["path_state"],
            related_slots=slot_numbers,
            evidence=route["path_evidence"],
        )

        enclosure_node = add_node(
            nodes,
            SasFabricNode(
                id=enclosure_id,
                kind=route["enclosure_kind"],
                label=route["enclosure_label"],
                raw_id=route.get("enclosure_raw_id"),
                controller_id=controller_id,
                related_slots=slot_numbers,
                metrics={
                    "slot_count": 1,
                    "selected_enclosure": True,
                    "fabric_kind": fabric_kind,
                    "source": route.get("source"),
                },
                evidence=route["enclosure_evidence"],
                raw=route["enclosure_raw"],
            ),
        )
        enclosure_node.metrics["slot_count"] = len(enclosure_node.related_slots)
        enclosure_nodes[enclosure_id] = enclosure_node
        add_link(
            links,
            path_id,
            enclosure_id,
            "path-storage-enclosure",
            status=route["path_state"],
            related_slots=slot_numbers,
            evidence=route["enclosure_evidence"],
        )

        bay_id = f"bay:{slot.slot}"
        backplane_zone = backplane_zones.get(slot.slot)
        bay_related_nodes = _dedupe_strings(
            [
                "host",
                controller_id,
                path_id,
                enclosure_id,
                *( [backplane_zone["id"]] if backplane_zone else [] ),
                bay_id,
            ]
        )
        bay_related_links = [
            _link_id("host", controller_id, "host-controller"),
            _link_id(controller_id, path_id, "controller-path"),
            _link_id(path_id, enclosure_id, "path-storage-enclosure"),
        ]
        add_node(
            nodes,
            SasFabricNode(
                id=bay_id,
                kind="bay",
                label=f"Bay {_bay_label(slot)}",
                slot=slot.slot,
                status=_slot_state_text(slot),
                related_slots=slot_numbers,
                metrics={
                    "present": _bay_present_metric(slot),
                    "pool_name": slot.pool_name,
                    "vdev_name": slot.vdev_name,
                    "device_name": slot.device_name,
                    "health": slot.health,
                    "transport_protocol": raw_status.get("transport_protocol") or raw_status.get("esxi_transport"),
                    "fabric_kind": fabric_kind,
                },
                evidence=list(_INVENTORY_SNAPSHOT_EVIDENCE),
                raw={
                    "source": route.get("source"),
                    "fabric_kind": fabric_kind,
                    "operator_context": dict(slot.operator_context or {}),
                    **(slot.raw_status if isinstance(slot.raw_status, dict) else {}),
                },
            ),
        )
        if backplane_zone:
            backplane_link = add_link(
                links,
                backplane_zone["id"],
                bay_id,
                "backplane-bay",
                slot=slot.slot,
                related_slots=slot_numbers,
                evidence=list(_PROFILE_SLOT_LAYOUT_EVIDENCE),
            )
            bay_related_links.append(backplane_link.id)
        enclosure_link = add_link(
            links,
            enclosure_id,
            bay_id,
            "storage-enclosure-bay",
            slot=slot.slot,
            related_slots=slot_numbers,
            evidence=route["enclosure_evidence"],
        )
        bay_related_links.append(enclosure_link.id)

        _add_pool_vdev_nodes_and_links(
            slot=slot,
            nodes=nodes,
            links=links,
            bay_id=bay_id,
            bay_related_nodes=bay_related_nodes,
            bay_related_links=bay_related_links,
        )

        traces[bay_id] = SasFabricTrace(
            id=bay_id,
            label=f"Bay {_bay_label(slot)}",
            kind="bay",
            node_ids=_dedupe_strings(bay_related_nodes),
            link_ids=_dedupe_strings(bay_related_links),
            slots=slot_numbers,
            metrics={
                "device_name": slot.device_name,
                "pool_name": slot.pool_name,
                "vdev_name": slot.vdev_name,
                "path_states": [
                    {
                        "controller": controller_name,
                        "controller_label": route["controller_label"],
                        "state": route["path_state"],
                        "member_state": route["path_state"],
                        "state_basis": "member-health-not-transport",
                        "device_name": slot.device_name,
                        "path_id": path_id,
                        "source": route.get("source"),
                        "path_type": route.get("path_type"),
                    }
                ],
                "mpr_devices": [],
                "fabric_kind": fabric_kind,
            },
            evidence=route["path_evidence"],
        )

    for path in paths_by_id.values():
        # Aggregate only after every member has contributed. These are member
        # health observations, not proof that a shared transport has failed.
        counts = dict(sorted(path["member_state_counts"].items()))
        known_states = set(counts) - {"unknown"}
        state = "mixed" if len(known_states) > 1 else "unknown" if "unknown" in counts else next(iter(counts))
        path["state"] = state
        path["member_state_counts"] = counts
        nodes[path["id"]].status = state
        nodes[path["id"]].metrics.update(
            member_state_counts=counts, state_basis=path["state_basis"],
        )
        traces[path["id"]] = SasFabricTrace(
            id=path["id"],
            label=path["label"],
            kind="path",
            node_ids=_dedupe_strings(["host", path["controller_id"], path["id"]]),
            link_ids=[
                _link_id("host", path["controller_id"], "host-controller"),
                _link_id(path["controller_id"], path["id"], "controller-path"),
            ],
            slots=path["slots"],
            metrics={
                "count": path["count"],
                "state": path["state"],
                "member_state_counts": counts,
                "state_basis": path["state_basis"],
                "source": path.get("source"),
                "fabric_kind": fabric_kind,
            },
            evidence=list(_INVENTORY_SNAPSHOT_EVIDENCE),
        )

    for link in links.values():
        if link.kind == "controller-path":
            link.status = paths_by_id[link.target]["state"]
        elif link.kind == "path-storage-enclosure":
            link.status = paths_by_id[link.source]["state"]

    controllers = []
    for controller in controllers_by_name.values():
        controller["path_counts"] = dict(controller["path_counts"])
        controllers.append(controller)
    paths = sorted(paths_by_id.values(), key=lambda item: (str(item.get("controller") or ""), str(item.get("label") or "")))
    aliases_by_id = _resolve_sas_fabric_alias_map(aliases_by_id, nodes)
    aliases_by_id = _resolve_platform_route_aliases(aliases_by_id, nodes, fabric_warnings)
    _apply_sas_fabric_aliases(
        nodes=nodes,
        traces=traces,
        controllers=controllers,
        paths=paths,
        aliases=aliases_by_id,
    )
    enclosures = [_node_raw_for_payload(node) for node in enclosure_nodes.values()]
    fabric_warnings.append(_storage_fabric_scope_warning(platform_label, fabric_kind))
    return _build_sas_fabric_result(
        available=True,
        system=system,
        snapshot=snapshot,
        nodes=nodes,
        links=links,
        traces=traces,
        controllers=controllers,
        expanders=[],
        enclosures=enclosures,
        paths=paths,
        aliases=list(aliases_by_id.values()),
        warnings=fabric_warnings,
        sources=sources,
        sort_controllers=True,
        raw={
            "fabric_domain": "storage_fabric",
            "fabric_kind": fabric_kind,
            "selected_bay_slots": selected_slots,
            "selected_disk_slots": selected_disk_slots,
            "command_failures": list(command_failures or []),
        },
    )


def normalize_path_state(value: str | None) -> str:
    state = (value or "unknown").strip().lower()
    if "active" in state:
        return "active"
    if "passive" in state:
        return "passive"
    if "fail" in state:
        return "fail"
    if "missing" in state:
        return "missing"
    return state or "unknown"


def add_node(nodes: dict[str, SasFabricNode], node: SasFabricNode) -> SasFabricNode:
    existing = nodes.get(node.id)
    if existing is None:
        nodes[node.id] = node
        return node
    existing.related_slots = _dedupe_ints([*existing.related_slots, *node.related_slots])
    existing.evidence = _dedupe_strings([*existing.evidence, *node.evidence])
    existing.metrics.update({key: value for key, value in node.metrics.items() if value is not None})
    legacy_ids = sorted(set(existing.raw.get("legacy_alias_ids", [])) | set(node.raw.get("legacy_alias_ids", [])))
    existing.raw.update({key: value for key, value in node.raw.items() if value is not None})
    if legacy_ids:
        existing.raw["legacy_alias_ids"] = legacy_ids
    if existing.status is None:
        existing.status = node.status
    return existing


def add_link(
    links: dict[str, SasFabricLink],
    source: str,
    target: str,
    kind: str,
    *,
    status: str | None = None,
    slot: int | None = None,
    related_slots: list[int] | None = None,
    evidence: list[str] | None = None,
) -> SasFabricLink:
    link_id = _link_id(source, target, kind, slot)
    existing = links.get(link_id)
    if existing is None:
        existing = SasFabricLink(
            id=link_id,
            source=source,
            target=target,
            kind=kind,
            status=status,
            slot=slot,
            related_slots=_dedupe_ints(related_slots or []),
            evidence=evidence or [],
        )
        links[link_id] = existing
        return existing
    existing.related_slots = _dedupe_ints([*existing.related_slots, *(related_slots or [])])
    existing.evidence = _dedupe_strings([*existing.evidence, *(evidence or [])])
    if existing.status is None:
        existing.status = status
    return existing


def _canonical_outputs(ssh_outputs: dict[str, str]) -> dict[str, str]:
    return {
        canonicalize_ssh_command(command): output
        for command, output in ssh_outputs.items()
        if output is not None
    }



def _pci_inventory_by_controller(
    pci_controllers: list[dict[str, Any]],
    pcie_slots: list[dict[str, Any]],
    sysctl_rows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    slots_by_address = {
        str(slot.get("bus_address") or "").lower(): slot
        for slot in pcie_slots
        if slot.get("bus_address")
    }
    inventory: dict[str, dict[str, Any]] = {}
    for row in pci_controllers:
        controller = normalize_text(row.get("controller"))
        if not controller:
            continue
        sysctl_row = sysctl_rows.get(controller, {})
        pci_address = str(row.get("pci_address") or sysctl_row.get("pci_address") or "").lower()
        slot = slots_by_address.get(pci_address, {})
        inventory[controller] = {
            **sysctl_row,
            **row,
            "pci_address": pci_address or row.get("pci_address"),
            "pcie_slot": slot.get("designation"),
            "pcie_slot_type": slot.get("type"),
            "pcie_slot_usage": slot.get("current_usage"),
            "pcie_slot_length": slot.get("length"),
            "pcie_slot_id": slot.get("slot_id"),
            "pcie_slot_characteristics": slot.get("characteristics") or [],
            "dmidecode_slot": slot or None,
        }
        if sysctl_row.get("pci_location"):
            inventory[controller]["sysctl_pci_location"] = sysctl_row.get("pci_location")
            inventory[controller]["sysctl_pci_address"] = sysctl_row.get("pci_address")
    for controller, sysctl_row in sysctl_rows.items():
        if controller in inventory:
            continue
        pci_address = str(sysctl_row.get("pci_address") or "").lower()
        slot = slots_by_address.get(pci_address, {})
        inventory[controller] = {
            **sysctl_row,
            "sysctl_pci_location": sysctl_row.get("pci_location"),
            "sysctl_pci_address": pci_address or None,
            "pci_address": pci_address or sysctl_row.get("pci_address"),
            "pcie_slot": slot.get("designation"),
            "pcie_slot_type": slot.get("type"),
            "pcie_slot_usage": slot.get("current_usage"),
            "pcie_slot_length": slot.get("length"),
            "pcie_slot_id": slot.get("slot_id"),
            "pcie_slot_characteristics": slot.get("characteristics") or [],
            "dmidecode_slot": slot or None,
        }
    return inventory


# Unit-data key for the unnumbered `mprutil show ...` fallback. It is not an MPR
# unit number: it maps to the controller name below, never to `mpr<unit>`.
_LEGACY_MPR_UNIT_KEY = -1
_LEGACY_MPR_CONTROLLER_NAME = "mpr"


def _mpr_controller_name_for_unit(unit: int) -> str:
    return f"mpr{unit}" if unit >= 0 else _LEGACY_MPR_CONTROLLER_NAME


def _mpr_controller_ids_by_unit(controllers: list[dict[str, Any]]) -> dict[int, str]:
    ids: dict[int, str] = {
        item["unit"]: item["id"]
        for item in controllers
        if item.get("unit") is not None
    }
    for item in controllers:
        if item.get("unit") is None and item.get("name") == _LEGACY_MPR_CONTROLLER_NAME:
            ids.setdefault(_LEGACY_MPR_UNIT_KEY, item["id"])
    return ids


def _parse_unit_data(outputs: dict[str, str]) -> dict[int, dict[str, Any]]:
    summary_units = set(discover_mpr_units_from_adapter_summary(outputs.get("mprutil show adapters", "")))
    units: set[int] = set(summary_units)
    for command in outputs:
        match = _MPR_UNIT_COMMAND_PATTERN.match(command)
        unit = admit_mpr_unit_number(match.group("unit")) if match and match.group("unit") is not None else None
        if unit is not None:
            units.add(unit)

    parsed: dict[int, dict[str, Any]] = {}
    for unit in sorted(units):
        payload = _parse_mpr_unit_payload(outputs, f"mprutil -u {unit} show")
        # A unit listed by the adapter summary was observed. A unit known only
        # from a command name needs output that parsed as controller data.
        if unit in summary_units or _mpr_unit_payload_observed(payload):
            parsed[unit] = payload
    # The unnumbered fallback is controller evidence only when its output
    # parsed as controller data; empty or unreadable text creates nothing.
    if not parsed:
        payload = _parse_mpr_unit_payload(outputs, "mprutil show")
        if _mpr_unit_payload_observed(payload):
            parsed[_LEGACY_MPR_UNIT_KEY] = payload
    return parsed


def _parse_mpr_unit_payload(outputs: dict[str, str], prefix: str) -> dict[str, Any]:
    # A dedicated reply wins; otherwise the same list's `show all` section.
    sections = _split_mpr_show_all(outputs.get(f"{prefix} all", ""))
    replies = {
        subcommand: outputs[f"{prefix} {subcommand}"] if f"{prefix} {subcommand}" in outputs else sections.get(subcommand)
        for subcommand in ("adapter", "devices", "enclosures", "expanders", "iocfacts")
    }
    # Replies the utility reported an error in: a `show all` section that was
    # used, and a dedicated reply, which gets the same qualification.
    failed = {name for name, text in sections.items() if text is None and f"{prefix} {name}" not in outputs}
    for subcommand in _MPR_SHOW_ALL_SECTIONS.values():
        if f"{prefix} {subcommand}" in outputs and _mpr_reply_reports_error(outputs[f"{prefix} {subcommand}"]):
            replies[subcommand] = None
            failed.add(subcommand)
    devices, rejected_device_rows = parse_mpr_device_list(replies["devices"] or "")
    payload: dict[str, Any] = {
        "adapter_detail": parse_mpr_adapter_detail(replies["adapter"] or ""),
        "devices": devices,
        "enclosures": parse_mpr_enclosures(replies["enclosures"] or ""),
        "expanders": parse_mpr_expanders(replies["expanders"] or ""),
        "iocfacts": parse_mpr_iocfacts(replies["iocfacts"] or ""),
    }
    if "adapter" in failed:
        # The utility reported an error in this reply: its PHY table was not read.
        payload["adapter_detail"]["phy_table"] = "rejected"
    payload["lists"] = {
        subcommand: _mpr_list_state(replies[subcommand], subcommand, payload[subcommand])
        for subcommand in _MPR_LIST_HEADINGS
    }
    if rejected_device_rows:
        # A device row that was not understood: the rows kept are not the whole list.
        payload["lists"]["devices"] = "unreadable"
    if rejected_mpr_expander_headers(replies["expanders"] or ""):
        # An expander header that was not admitted: the expanders kept are
        # not the whole list (#1416).
        payload["lists"]["expanders"] = "unreadable"
    return payload


# `mprutil show all` prints these labels, each alone on a line, before the
# reply of the command it runs. Text under any other label is not read.
_MPR_SHOW_ALL_SECTIONS = {"Adapter:": "adapter", "Devices:": "devices", "Enclosures:": "enclosures", "Expanders:": "expanders"}
# Any column-0 line ending in a colon ends a section, except the adapter
# reply's own "mpr0 Adapter:" name line.
_MPR_SHOW_ALL_LABEL = re.compile(r"\S.*:")
_MPR_ADAPTER_NAME_LINE = re.compile(r"\S+ Adapter:")
# mprutil's warn() lines: "mprutil: Error retrieving enclosure page: ...".
_MPR_UTILITY_ERROR = re.compile(r"mp[rs]util: ")


def _mpr_reply_reports_error(text: str) -> bool:
    return any(_MPR_UTILITY_ERROR.match(line.strip()) for line in text.splitlines())


def _split_mpr_show_all(text: str) -> dict[str, str | None]:
    sections: dict[str, list[str]] = {}
    failed: set[str] = set()
    current: str | None = None
    for line in text.splitlines():
        if current is not None and _MPR_UTILITY_ERROR.match(line.strip()):
            failed.add(current)
            continue
        # Labels start at column 0; indented `Key:` lines belong to a reply.
        label = line.rstrip()
        if _MPR_SHOW_ALL_LABEL.fullmatch(label) and not (current == "adapter" and _MPR_ADAPTER_NAME_LINE.fullmatch(label)):
            current = _MPR_SHOW_ALL_SECTIONS.get(label)
            if current is not None:
                sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    # A label with nothing under it is a command that printed nothing, and a
    # section the utility reported an error in was not read whole: not read.
    return {
        name: None if name in failed else "\n".join(lines) + "\n"
        for name, lines in sections.items()
        if name in failed or any(line.strip() for line in lines)
    }


# mprutil prints a column heading before each list, even an empty one.
_MPR_LIST_HEADINGS = {
    "expanders": re.compile(r"(?i)(?:num\s*phys|phy\s+remotephy)\b"),
    "enclosures": re.compile(r"(?i)slots\s+logical\s+id\b"),
    "devices": re.compile(r"(?i)b_+t\b"),
}


def _mpr_list_state(text: str | None, subcommand: str, rows: list[Any]) -> str:
    """read, empty (a measured zero), unreadable, or not_read when no reply was kept."""
    if text is None:
        return "not_read"
    if rows:
        return "read"
    heading = _MPR_LIST_HEADINGS[subcommand]
    if all(not line.strip() or heading.match(line.strip()) for line in text.splitlines()):
        return "empty"
    # Text that parses to nothing is not an empty list.
    return "unreadable"


def _mpr_unit_payload_observed(payload: dict[str, Any]) -> bool:
    """True only for output that parsed as recognised controller data.

    Free ``key: value`` text alone (generic adapter keys, iocfacts) is not
    enough: any unrelated text containing a colon would otherwise count.
    """
    detail = payload.get("adapter_detail") or {}
    return bool(
        detail.get("board_name")
        or detail.get("firmware_revision")
        or detail.get("phy_rows")
        or payload.get("devices")
        or payload.get("enclosures")
        or payload.get("expanders")
    )


# Every supported unit command, `show all` included. The unit token is
# admitted separately (1 to 4 ASCII digits); anything else is a rejected unit.
_MPR_UNIT_COMMAND_PATTERN = re.compile(
    r"mprutil (?:-u (?P<unit>\S+) )?show (?P<subcommand>adapter|all|devices|enclosures|expanders|iocfacts)$"
)
# A failed read reaches the builder as a record that names no command: a fixed
# context and, for a controller read, the controller. These two say which
# records are controller reads; they must stay in step with the contexts the
# inventory writes for the commands the pattern above matches.
_MPR_UNIT_READ_FAILURE_CONTEXT = re.compile(r"sas_fabric_mprutil_(?:adapter|all|devices|enclosures|expanders|iocfacts)")
_MPR_UNNUMBERED_READ_FAILURE_CONTEXT = "sas_fabric_mprutil"
_MPR_FAILURE_CONTROLLER = re.compile(r"mpr(?P<unit>\d+)")
_MPR_READ_NAME_LIMIT = 8
# Stands for the unit tokens of failed reads whose unit was not admitted; no
# unit token contains a space, so it cannot equal one.
_REJECTED_FAILED_READ_UNIT = "rejected failed read"


def _bounded_name_list(names: list[str]) -> str:
    shown = names[:_MPR_READ_NAME_LIMIT]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _mpr_unit_read_results(
    outputs: dict[str, str],
    unit_data: dict[int, dict[str, Any]],
    command_failures: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, str]], int]:
    """One result per attempted controller unit (read, unreadable, no_output or
    command_failed), and how many unit tokens were rejected.

    A unit was attempted when the adapter summary lists it or one of its unit
    commands answered or failed. The unnumbered fallback counts only when no
    numbered unit was read, because only then is it consulted.
    """
    summary = outputs.get("mprutil show adapters", "")
    attempted: dict[int, dict[str, bool]] = {
        unit: {"text": False, "failed": False} for unit in discover_mpr_units_from_adapter_summary(summary)
    }
    rejected = rejected_mpr_summary_unit_tokens(summary)

    def note(token: str | None, *, text: bool, failed: bool) -> None:
        unit = _LEGACY_MPR_UNIT_KEY if token is None else admit_mpr_unit_number(token)
        if unit is None:
            rejected.add(token)
            return
        entry = attempted.setdefault(unit, {"text": False, "failed": False})
        entry["text"] = entry["text"] or text
        entry["failed"] = entry["failed"] or failed

    for command, value in outputs.items():
        match = _MPR_UNIT_COMMAND_PATTERN.match(command)
        if match:
            note(match.group("unit"), text=bool(value.strip()), failed=False)
    for failure in command_failures or []:
        context, controller = failure.get("context"), failure.get("controller")
        if (
            isinstance(context, str)
            and _MPR_UNIT_READ_FAILURE_CONTEXT.fullmatch(context)
            and controller is None
            and failure.get("controller_rejected") == MPR_REJECTED_CONTROLLER_REASON
        ):
            # The record keeps no token, so every such record is one rejected
            # unit between them: a lower bound, never zero.
            rejected.add(_REJECTED_FAILED_READ_UNIT)
            continue
        if not isinstance(context, str) or not isinstance(controller, str):
            continue
        if context == _MPR_UNNUMBERED_READ_FAILURE_CONTEXT and controller == _LEGACY_MPR_CONTROLLER_NAME:
            note(None, text=False, failed=True)
        elif _MPR_UNIT_READ_FAILURE_CONTEXT.fullmatch(context):
            numbered = _MPR_FAILURE_CONTROLLER.fullmatch(controller)
            if numbered:
                note(numbered.group("unit"), text=False, failed=True)

    numbered_read = any(unit >= 0 for unit in unit_data)
    results: list[dict[str, str]] = []
    for unit in sorted(attempted):
        if unit == _LEGACY_MPR_UNIT_KEY and numbered_read:
            continue
        entry = attempted[unit]
        if unit in unit_data:
            result = "read"
        elif entry["text"]:
            result = "unreadable"
        elif entry["failed"]:
            result = "command_failed"
        else:
            result = "no_output"
        results.append({"controller": _mpr_controller_name_for_unit(unit), "result": result})
    return results, len(rejected)


def _mpr_units_missing_list(unit_data: dict[int, dict[str, Any]], subcommand: str) -> list[str]:
    """Read units whose `show <subcommand>` reply was not read (failed, never ran or unreadable)."""
    return [
        _mpr_controller_name_for_unit(unit)
        for unit, payload in sorted(unit_data.items())
        if payload["lists"][subcommand] not in {"read", "empty"}
    ]


def _mpr_count_completeness(
    unit_data: dict[int, dict[str, Any]],
    unit_reads: list[dict[str, str]],
    *,
    rejected_units: int,
    controllers_seen: bool,
    paths_seen: bool,
) -> dict[str, str]:
    """complete, partial (a lower bound) or not_read for each controller-side count."""
    if not unit_data:
        mpr_state = "not_read"
    elif rejected_units or any(item["result"] != "read" for item in unit_reads):
        mpr_state = "partial"
    else:
        mpr_state = "complete"

    def listed(subcommand: str) -> str:
        if mpr_state != "complete":
            return mpr_state
        # A read unit whose list command did not answer leaves the count short.
        return "partial" if _mpr_units_missing_list(unit_data, subcommand) else "complete"

    return {
        "controllers": "partial" if mpr_state == "not_read" and controllers_seen else mpr_state,
        # Paths come from the inventory's multipath members; with no controller
        # data and no paths at all, the producer cannot tell 0 from unread.
        "paths": "not_read" if mpr_state == "not_read" and not controllers_seen and not paths_seen else "complete",
        "expanders": listed("expanders"),
        "enclosures": listed("enclosures"),
    }


# A CAM controller (SIM) name as `camcontrol devlist -v` prints it: a lowercase
# FreeBSD driver name (mpr, mps, mpt, isp, umass-sim, mpi3mr, ...) ending in a
# letter, then a 1-4 digit unit (the unit rule of `admit_mpr_unit_number`). The
# length is checked before any regex. Being a valid path label does not make a
# controller eligible for MPR diagnostics; those stay keyed by mprutil units.
_CORE_CONTROLLER_NAME = re.compile(r"[a-z](?:[a-z0-9_-]{0,10}[a-z_])?[0-9]{1,4}")
_CORE_CONTROLLER_NAME_MAX_LENGTH = 16


def _core_controller_name(value: Any) -> str | None:
    """A member's controller name, or None (not read) when it is not one."""
    if not isinstance(value, str) or len(value) > _CORE_CONTROLLER_NAME_MAX_LENGTH:
        return None
    return value if _CORE_CONTROLLER_NAME.fullmatch(value) else None


def _multipath_member_not_read(member: MultipathMember, observed: MultipathMember) -> dict[str, Any]:
    """A controller label the guard refused is recorded as not read."""
    return {"not_read": ["controller"]} if observed.controller_label and not member.controller_label else {}


def _core_path_members(
    slots: list[SlotView], cam_devices: Any, disk_lookup_keys: tuple[frozenset[str], ...] | None = None,
) -> dict[int, list[MultipathMember]]:
    """Use the inventory's shared-bay rule when no geom lists the disk's paths.

    Peers must pass the rule twice: against every API disk the inventory read
    (including unplaced disks and other enclosures), exactly as the enclosure
    view does, and against the rendered bays, so a name another bay claims is
    not borrowed. Without the API index no peer is admitted.
    """
    # Last writer wins, as in inventory's `_index_disk_records`. Each disk's key
    # set is its owner identity.
    api_owners: dict[str, frozenset[str]] = {
        key: keys for keys in (disk_lookup_keys or ()) for key in keys
    }
    owners: dict[str, Any] = {}
    conflicting_owner = object()
    for slot in slots:
        raw_names = (slot.raw_status or {}).get("device_names")
        for value in [
            slot.device_name, *slot.smart_device_names,
            *(raw_names if isinstance(raw_names, list) else []),
            *([member.device_name for member in slot.multipath.members] if slot.multipath else []),
        ]:
            name = normalize_device_name(value) if isinstance(value, str) else None
            if name:
                key = name.lower()
                if key not in owners:
                    owners[key] = slot
                elif owners[key] is not slot:
                    owners[key] = conflicting_owner
    result: dict[int, list[MultipathMember]] = {}
    for slot in slots:
        if slot.multipath and slot.multipath.members:
            result[slot.slot] = [
                member.model_copy(update={"controller_label": _core_controller_name(member.controller_label)})
                for member in slot.multipath.members
            ]
            continue
        members: list[MultipathMember] = []
        result[slot.slot] = members
        own = normalize_device_name(slot.device_name)
        raw = slot.raw_status or {}
        names = raw.get("device_names")
        if (
            slot_occupancy(slot) != "present" or not slot.physical_location_known or not _slot_ses_devices(slot)
            or not own or not re.fullmatch(r"da[0-9]+", own)
            or not isinstance(names, list) or owners.get(own.lower()) is not slot
        ):
            continue
        # The helper requires this bay to name the disk's own path, and rejects
        # peers another disk owns. It is also used by the enclosure view (#902).
        api_owner = api_owners.get(own.lower())
        peers = set(bay_peer_devices(raw, {own.lower()}, owners, slot)).intersection(
            bay_peer_devices(raw, set(api_owner), api_owners, api_owner)
        ) if api_owner is not None else set()
        listed = list(dict.fromkeys(
            name for value in names if isinstance(value, str)
            if (name := normalize_device_name(value)) and re.fullmatch(r"da[0-9]+", name)
        ))
        if own not in listed:
            continue
        # Only a hint naming an admitted path establishes current pool usage.
        # The API's preferred device and a stale hint do not establish it.
        in_use = normalize_device_name(raw.get("pool_device_name"))
        if in_use not in {name for name in listed if name == own or name in peers}:
            in_use = None
        for name in listed:
            if name != own and name not in peers:
                continue
            members.append(MultipathMember(
                device_name=name,
                controller_label=_core_controller_name(cam_devices.controllers.get(name.lower())),
                state="unknown" if in_use is None else "active" if name == in_use else "standby",
            ))
    return result


def _direct_member_not_read(member: MultipathMember, cam_devices: Any) -> dict[str, Any]:
    not_read = ["state"] if member.state == "unknown" else []
    if not member.controller_label:
        not_read.append("controller")
    if _cam_member_target(cam_devices, member.controller_label or "", member.device_name) is None:
        not_read.append("target")
    return {"not_read": not_read} if not_read else {}


def _slot_path_evidence(slot: SlotView) -> list[str]:
    return list(_SLOT_MULTIPATH_EVIDENCE) if slot.multipath and slot.multipath.members else [
        "sesutil map shared bay", "camcontrol devlist -v",
    ]


def _path_counts_from_slots(
    slots: list[SlotView], members_by_slot: dict[int, list[MultipathMember]] | None = None,
) -> tuple[dict[str, Counter[str]], dict[str, dict[str, list[int]]]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    path_slots: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for slot in slots:
        members = members_by_slot[slot.slot] if members_by_slot is not None else (slot.multipath.members if slot.multipath else [])
        for member in members:
            controller = normalize_text(member.controller_label)
            if not controller:
                continue
            state = normalize_path_state(member.state)
            counts[controller][state] += 1
            path_slots[controller][state].append(slot.slot)
    return dict(counts), {controller: dict(states) for controller, states in path_slots.items()}


def _ses_devices_by_slot(slots: list[SlotView]) -> dict[int, list[str]]:
    devices: dict[int, list[str]] = {}
    for slot in slots:
        candidates = _slot_ses_devices(slot)
        if candidates:
            devices[slot.slot] = candidates
    return devices


def _slot_ses_devices(slot: SlotView) -> list[str]:
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    candidates: list[str] = []
    if slot.ssh_ses_device:
        candidates.append(slot.ssh_ses_device)
    for target in slot.ssh_ses_targets:
        ses_device = target.get("ses_device") if isinstance(target, dict) else None
        if isinstance(ses_device, str) and ses_device:
            candidates.append(ses_device)
    for key in ("ses_device", "ssh_ses_device"):
        value = raw_status.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    return _dedupe_strings(candidates)


def _slot_primary_ses_device(slot: SlotView) -> str | None:
    devices = _slot_ses_devices(slot)
    return devices[0] if devices else None


def _snapshot_has_linux_ses_evidence(snapshot: InventorySnapshot) -> bool:
    return any(_slot_ses_devices(slot) for slot in snapshot.slots)


def _platform_label(platform: str | None) -> str:
    normalized = normalize_text(platform).lower()
    return {
        "core": "TrueNAS CORE",
        "scale": "TrueNAS SCALE",
        "quantastor": "Quantastor",
        "linux": "Linux",
        "esxi": "ESXi",
        "ipmi": "BMC / IPMI",
    }.get(normalized, normalized.upper() if normalized else "This platform")


def _storage_fabric_kind(platform: str | None) -> str:
    normalized = normalize_text(platform).lower()
    return {
        "scale": "storage_scale",
        "linux": "storage_linux",
        "quantastor": "storage_quantastor",
        "esxi": "storage_esxi",
        "ipmi": "storage_bmc",
    }.get(normalized, "storage_generic")


def _storage_fabric_scope_warning(platform_label: str, fabric_kind: str) -> str:
    if fabric_kind == "storage_quantastor":
        return (
            f"{platform_label} Storage Fabric is built from Quantastor storage-system, HA-node, pool, disk and "
            "optional SES data; controller and expander hops appear only when those sources report them."
        )
    if fabric_kind == "storage_esxi":
        return (
            f"{platform_label} Storage Fabric is built from ESXCLI and vendor controller data; "
            "it shows host, controller and member relationships and never changes RAID settings."
        )
    if fabric_kind in {"storage_linux", "storage_scale"}:
        return (
            f"{platform_label} Storage Fabric is built from Linux block, pool, profile, SMART and optional SES data; "
            "HBA and expander hops are not shown."
        )
    if fabric_kind == "storage_bmc":
        return (
            f"{platform_label} Storage Fabric is built from BMC slot and chassis data only; "
            "host storage paths need OS or vendor storage data."
        )
    return (
        f"{platform_label} Storage Fabric is built from the data this platform reports; "
        "hops it does not report are left out rather than guessed."
    )


def _bay_present_metric(slot: SlotView) -> bool | str:
    """The bay's `present` metric: yes, no, or "unknown" when it was not read (#1218)."""
    occupancy = slot_occupancy(slot)
    return "unknown" if occupancy == "unknown" else occupancy == "present"


def _slot_has_storage_fabric_evidence(slot: SlotView) -> bool:
    if _slot_has_disk(slot) or slot_occupancy(slot) == "present" or slot.smart_device_names:
        return True
    if isinstance(slot.operator_context, dict) and any(_truthy_storage_value(value) for value in slot.operator_context.values()):
        return True
    if isinstance(slot.raw_status, dict) and any(_truthy_storage_value(value) for value in slot.raw_status.values()):
        return True
    return False


def _truthy_storage_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if value == "" or value == [] or value == {}:
        return False
    return True


def _slot_state_text(slot: SlotView) -> str:
    if slot.health:
        return normalize_text(slot.health).lower() or "unknown"
    if hasattr(slot.state, "value"):
        return normalize_text(slot.state.value).lower() or "unknown"
    return normalize_text(str(slot.state)).lower() or "unknown"


def _slot_device_candidates(slot: SlotView) -> list[str]:
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    raw_device_names = raw_status.get("device_names") if isinstance(raw_status.get("device_names"), list) else []
    return _dedupe_strings(
        [
            slot.device_name,
            *slot.smart_device_names,
            *raw_device_names,
            raw_status.get("device_hint"),
            slot.logical_unit_id,
            slot.sas_address,
            raw_status.get("esxi_device_id"),
            raw_status.get("esxi_runtime_name"),
            raw_status.get("storcli_slot"),
        ]
    )


def _pool_vdev_label(slot: SlotView, fallback: str = "storage path") -> str:
    if slot.topology_label:
        return slot.topology_label
    if slot.pool_name and slot.vdev_name:
        return f"{slot.pool_name} / {slot.vdev_name}"
    if slot.pool_name:
        return slot.pool_name
    if slot.vdev_name:
        return slot.vdev_name
    devices = _slot_device_candidates(slot)
    return devices[0] if devices else fallback


@dataclass(frozen=True)
class StorageFabricRouteContext:
    system: SystemConfig
    snapshot: InventorySnapshot
    slot: SlotView
    raw_status: dict[str, Any]
    operator_context: dict[str, Any]
    fabric_kind: str
    platform_label: str
    platform: str


class StorageFabricRouteProvider(Protocol):
    key: str

    def matches(self, context: StorageFabricRouteContext) -> bool:
        ...

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        ...


class QuantastorStorageRouteProvider:
    key = "quantastor"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return context.platform == "quantastor"

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _quantastor_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.operator_context,
            context.fabric_kind,
        )


class EsxiStorageRouteProvider:
    key = "esxi"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return context.platform == "esxi"

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _esxi_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.fabric_kind,
        )


class BmcStorageRouteProvider:
    key = "bmc"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return context.platform == "ipmi"

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _bmc_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.fabric_kind,
        )


class LinuxStorageRouteProvider:
    key = "linux"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return True

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _linux_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.fabric_kind,
            context.platform_label,
        )


_PLATFORM_STORAGE_ROUTE_PROVIDERS: tuple[StorageFabricRouteProvider, ...] = (
    QuantastorStorageRouteProvider(),
    EsxiStorageRouteProvider(),
    BmcStorageRouteProvider(),
    LinuxStorageRouteProvider(),
)


def _build_storage_fabric_route_context(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    fabric_kind: str,
    platform_label: str,
) -> StorageFabricRouteContext:
    return StorageFabricRouteContext(
        system=system,
        snapshot=snapshot,
        slot=slot,
        raw_status=slot.raw_status if isinstance(slot.raw_status, dict) else {},
        operator_context=slot.operator_context if isinstance(slot.operator_context, dict) else {},
        fabric_kind=fabric_kind,
        platform_label=platform_label,
        platform=normalize_text(system.truenas.platform).lower(),
    )


def _select_storage_fabric_route_provider(context: StorageFabricRouteContext) -> StorageFabricRouteProvider:
    for provider in _PLATFORM_STORAGE_ROUTE_PROVIDERS:
        if provider.matches(context):
            return provider
    raise RuntimeError(f"No Storage Fabric route provider registered for {context.platform!r}")


def _storage_fabric_route_for_slot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    fabric_kind: str,
    platform_label: str,
) -> dict[str, Any]:
    context = _build_storage_fabric_route_context(
        system=system,
        snapshot=snapshot,
        slot=slot,
        fabric_kind=fabric_kind,
        platform_label=platform_label,
    )
    route = _select_storage_fabric_route_provider(context).route(context)
    legacy_controller_id = route["controller_id"]
    legacy_path_id = route["path_id"]
    controller_identity = [route["source"], route["controller_identity"]]
    route["controller_id"] = _platform_route_id("controller", controller_identity)
    route["path_id"] = _platform_route_id(
        "path", [*controller_identity, route["path_identity"], slot.pool_name, slot.vdev_name],
    )
    # The enclosure belongs to the same controller scope, even when the old
    # normalized controller names collide.
    legacy_enclosure_id = route["enclosure_id"]
    route["enclosure_id"] = route["enclosure_id"].replace(
        f":{route['controller_name']}:", f":{route['controller_id'].removeprefix('controller:')}:", 1,
    )
    route["controller_name"] = route["controller_id"].removeprefix("controller:")
    route["enclosure_raw"]["legacy_alias_ids"] = [legacy_enclosure_id]
    route["controller_raw"]["legacy_alias_ids"] = [legacy_controller_id]
    route["path_raw"]["legacy_alias_ids"] = [legacy_path_id]
    return route


def _platform_route_id(kind: str, components: list[Any]) -> str:
    """Versioned, case-preserving tuple identity, independent of row order.

    JSON keeps absent components distinct from literal labels. SHA-256 keeps
    IDs within the alias API's 256-character limit even for long labels, without
    case folding or delimiter substitution. No row ordinal enters the identity.
    """
    digest = hashlib.sha256(json.dumps(components, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"{kind}:storage-v2:{digest}"


def _quantastor_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    operator_context: dict[str, Any],
    fabric_kind: str,
) -> dict[str, Any]:
    selected_label = (
        normalize_text(operator_context.get("selected_view_label"))
        or normalize_text(snapshot.selected_enclosure_label)
        or normalize_text(system.label)
        or system.id
    )
    controller_name = _object_id_token(f"quantastor-{selected_label}")
    ses_device = _slot_primary_ses_device(slot) or normalize_text(raw_status.get("ses_device"))
    owner_label = normalize_text(operator_context.get("pool_owner_label"))
    fence_label = normalize_text(operator_context.get("fence_owner_label"))
    path_anchor = ses_device or owner_label or _pool_vdev_label(slot, selected_label)
    path_label = ses_device or _pool_vdev_label(slot, selected_label)
    evidence = ["Quantastor REST", "inventory snapshot"]
    if raw_status.get("quantastor_cli_disk"):
        evidence.append("qs disk-list")
    if raw_status.get("quantastor_hw_disk"):
        evidence.append("qs hw-disk-list")
    if ses_device:
        evidence.append("sg_ses AES/EC")
    enclosure_kind = "ses-enclosure" if ses_device else "storage-enclosure"
    enclosure_token = _object_id_token(snapshot.selected_enclosure_id or selected_label)
    return {
        "source": "quantastor",
        "controller_identity": selected_label,
        "path_identity": ses_device or owner_label or (None if slot.pool_name or slot.vdev_name else path_anchor),
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": selected_label,
        "controller_device": operator_context.get("selected_view_label") or snapshot.selected_enclosure_id,
        "controller_status": "online",
        "controller_evidence": _dedupe_strings(evidence),
        "controller_raw": {
            "selected_view_label": selected_label,
            "pool_owner_label": owner_label,
            "fence_owner_label": fence_label,
            "visible_on_labels": operator_context.get("visible_on_labels"),
            "io_fencing_enabled": operator_context.get("io_fencing_enabled"),
        },
        "path_id": f"path:{controller_name}:{_object_id_token(path_anchor)}",
        "path_label": path_label,
        "path_state": _slot_state_text(slot),
        "path_type": "quantastor-ha" if operator_context else "quantastor",
        "path_evidence": _dedupe_strings(evidence),
        "path_raw": {
            "source": "quantastor",
            "fabric_kind": fabric_kind,
            "ses_device": ses_device,
            "pool_owner_label": owner_label,
            "fence_owner_label": fence_label,
            "visible_on_labels": operator_context.get("visible_on_labels"),
            "topology_label": slot.topology_label,
        },
        "enclosure_id": f"{enclosure_kind}:{controller_name}:{enclosure_token}",
        "enclosure_kind": enclosure_kind,
        "enclosure_label": snapshot.selected_enclosure_label or selected_label,
        "enclosure_raw_id": ses_device or snapshot.selected_enclosure_id,
        "enclosure_evidence": list(_SG_SES_AES_EVIDENCE) if ses_device else ["Quantastor REST"],
        "enclosure_raw": {
            "source": "quantastor",
            "fabric_kind": fabric_kind,
            "ses_device": ses_device,
            "enclosure_id": snapshot.selected_enclosure_id,
            "enclosure_label": snapshot.selected_enclosure_label,
        },
    }


def _esxi_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    fabric_kind: str,
) -> dict[str, Any]:
    drive = raw_status.get("storcli_physical_drive") if isinstance(raw_status.get("storcli_physical_drive"), dict) else {}
    controller = normalize_text(drive.get("controller_id") or raw_status.get("controller_id") or "storage")
    controller_name = _object_id_token(f"esxi-{controller}")
    controller_label = f"StorCLI {controller}" if controller != "storage" else "ESXi storage"
    connector = normalize_text(
        drive.get("connector_name")
        or drive.get("connected_port")
        or raw_status.get("esxi_runtime_name")
        or slot.vdev_name
        or _pool_vdev_label(slot, "ESXi local storage")
    )
    path_label = connector or _pool_vdev_label(slot, "ESXi local storage")
    evidence = ["ESXCLI storage", "inventory snapshot"]
    if drive:
        evidence.append("StorCLI physical drive")
    if raw_status.get("esxi_smart"):
        evidence.append("esxcli storage core device smart get")
    enclosure_token = _object_id_token(
        raw_status.get("storcli_enclosure_id")
        or drive.get("enclosure_id")
        or drive.get("eid")
        or snapshot.selected_enclosure_id
        or "esxi-local"
    )
    return {
        "source": "esxi",
        "controller_identity": controller,
        "path_identity": connector,
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": controller_label,
        "controller_device": drive.get("controller_id") or controller,
        "controller_status": "online" if drive else "mapped",
        "controller_evidence": _dedupe_strings(evidence),
        "controller_raw": {
            "controller_id": drive.get("controller_id") or controller,
            "storcli_controller": drive.get("controller_id"),
        },
        "path_id": f"path:{controller_name}:{_object_id_token(path_label)}",
        "path_label": path_label,
        "path_state": (normalize_text(drive.get("state") or "") or "").lower() or _slot_state_text(slot),
        "path_type": "storcli-member" if drive else "esxi-storage",
        "path_evidence": _dedupe_strings(evidence),
        "path_raw": {
            "source": "esxi",
            "fabric_kind": fabric_kind,
            "connector_name": drive.get("connector_name"),
            "connected_port": drive.get("connected_port"),
            "esxi_runtime_name": raw_status.get("esxi_runtime_name"),
            "esxi_device_id": raw_status.get("esxi_device_id"),
            "storcli_slot": drive.get("slot_key") or raw_status.get("storcli_slot"),
        },
        "enclosure_id": f"storage-enclosure:{controller_name}:{enclosure_token}",
        "enclosure_kind": "storage-enclosure",
        "enclosure_label": snapshot.selected_enclosure_label or f"ESXi enclosure {enclosure_token}",
        "enclosure_raw_id": raw_status.get("storcli_enclosure_id") or drive.get("enclosure_id"),
        "enclosure_evidence": ["StorCLI physical drive"] if drive else ["ESXCLI storage"],
        "enclosure_raw": {
            "source": "esxi",
            "fabric_kind": fabric_kind,
            "enclosure_id": raw_status.get("storcli_enclosure_id") or drive.get("enclosure_id"),
            "enclosure_label": snapshot.selected_enclosure_label,
        },
    }


def _linux_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    fabric_kind: str,
    platform_label: str,
) -> dict[str, Any]:
    devices = _slot_device_candidates(slot)
    device_text = " ".join(devices).lower()
    topology_text = " ".join([slot.topology_label or "", slot.vdev_name or "", slot.pool_name or ""]).lower()
    if "nvme" in device_text or "nvme" in topology_text:
        source_name = "linux-nvme"
        source_label = f"{platform_label} NVMe"
        source = "linux_nvme"
        evidence = ["lsblk", "smartctl", "nvme-cli"]
        path_type = "nvme"
    elif re.search(r"\bmd\d+\b", device_text) or "mdadm" in topology_text or re.search(r"\bmd\d+\b", topology_text):
        source_name = "linux-mdadm"
        source_label = f"{platform_label} mdadm"
        source = "linux_mdadm"
        evidence = ["lsblk", "mdadm", "smartctl"]
        path_type = "mdadm"
    elif fabric_kind == "storage_scale":
        source_name = "scale-storage"
        source_label = "TrueNAS SCALE storage"
        source = "scale_storage"
        evidence = ["TrueNAS SCALE API", "Linux storage"]
        path_type = "scale-storage"
    else:
        source_name = "linux-block"
        source_label = f"{platform_label} block"
        source = "linux_block"
        evidence = ["lsblk", "smartctl"]
        path_type = "block"
    controller_name = _object_id_token(source_name)
    path_label = _pool_vdev_label(slot, devices[0] if devices else source_label)
    enclosure_label = snapshot.selected_enclosure_label or snapshot.selected_enclosure_name or "Storage view"
    return {
        "source": source,
        "controller_identity": source_name,
        "path_identity": None if slot.pool_name or slot.vdev_name else path_label,
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": source_label,
        "controller_device": devices[0] if devices else source_name,
        "controller_status": "online",
        "controller_evidence": _dedupe_strings([*evidence, "inventory snapshot"]),
        "controller_raw": {"devices": devices, "source": source},
        "path_id": f"path:{controller_name}:{_object_id_token(path_label)}",
        "path_label": path_label,
        "path_state": _slot_state_text(slot),
        "path_type": path_type,
        "path_evidence": _dedupe_strings([*evidence, "inventory snapshot"]),
        "path_raw": {
            "source": source,
            "fabric_kind": fabric_kind,
            "devices": devices,
            "topology_label": slot.topology_label,
        },
        "enclosure_id": f"storage-enclosure:{controller_name}:{_object_id_token(snapshot.selected_enclosure_id or enclosure_label)}",
        "enclosure_kind": "storage-enclosure",
        "enclosure_label": enclosure_label,
        "enclosure_raw_id": snapshot.selected_enclosure_id,
        "enclosure_evidence": (
            ["inventory snapshot", "virtual inventory", "physical location unavailable"]
            if not slot.physical_location_known else ["profile/storage view"]
        ),
        "enclosure_raw": {
            "source": source,
            "fabric_kind": fabric_kind,
            "enclosure_id": snapshot.selected_enclosure_id,
            "enclosure_label": enclosure_label,
        },
    }


def _bmc_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    fabric_kind: str,
) -> dict[str, Any]:
    controller_name = "bmc-ipmi"
    chassis_label = snapshot.selected_enclosure_label or system.label or "BMC chassis"
    path_label = raw_status.get("bmc_controller_id")
    if path_label is not None:
        path_label = f"BMC controller {path_label}"
    else:
        path_label = "BMC slot inventory"
    return {
        "source": "bmc",
        "controller_identity": controller_name,
        "path_identity": raw_status.get("bmc_controller_id"),
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": "BMC / IPMI",
        "controller_device": raw_status.get("bmc_controller_id") or "ipmi",
        "controller_status": "present" if slot_occupancy(slot) == "present" else "mapped",
        "controller_evidence": list(_BMC_INVENTORY_EVIDENCE),
        "controller_raw": {
            "bmc_controller_id": raw_status.get("bmc_controller_id"),
            "bmc_physical_index": raw_status.get("bmc_physical_index"),
        },
        "path_id": f"path:{controller_name}:{_object_id_token(path_label)}",
        "path_label": path_label,
        "path_state": "present" if slot_occupancy(slot) == "present" else "mapped",
        "path_type": "bmc-slot",
        "path_evidence": list(_BMC_INVENTORY_EVIDENCE),
        "path_raw": {
            "source": "bmc",
            "fabric_kind": fabric_kind,
            "bmc_slot_number": raw_status.get("bmc_slot_number"),
            "bmc_physical_index": raw_status.get("bmc_physical_index"),
        },
        "enclosure_id": f"storage-enclosure:{controller_name}:{_object_id_token(snapshot.selected_enclosure_id or chassis_label)}",
        "enclosure_kind": "storage-enclosure",
        "enclosure_label": chassis_label,
        "enclosure_raw_id": snapshot.selected_enclosure_id,
        "enclosure_evidence": list(_BMC_INVENTORY_EVIDENCE),
        "enclosure_raw": {
            "source": "bmc",
            "fabric_kind": fabric_kind,
            "enclosure_id": snapshot.selected_enclosure_id,
            "enclosure_label": chassis_label,
        },
    }


def slots_overlap(left: list[int], right: list[int]) -> bool:
    if not left or not right:
        return False
    return bool(set(left).intersection(right))


def _selected_enclosure_keys(snapshot: InventorySnapshot) -> set[str]:
    keys: set[str] = set()
    for value in (snapshot.selected_enclosure_id,):
        if not value:
            continue
        for part in re.split(r"[+,\s]+", str(value)):
            key = _identifier_lookup_key(part)
            if key and re.fullmatch(r"[0-9a-f]{8,}", key):
                keys.add(key)
    return keys


def _snapshot_slot_numbers(slots: list[SlotView]) -> list[int]:
    return _dedupe_ints(slot.slot for slot in slots if slot.slot is not None)


def _snapshot_disk_slot_numbers(slots: list[SlotView]) -> list[int]:
    return _dedupe_ints(slot.slot for slot in slots if slot.slot is not None and _slot_has_disk(slot))


def _object_id_token(value: Any) -> str:
    text = normalize_text(str(value or ""))
    if text.startswith("/dev/"):
        text = text.removeprefix("/dev/")
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-").lower()
    return token or "unknown"


def _storage_node_id_component(value: Any) -> str:
    text = normalize_text(str(value or "")) or "unknown"
    return quote(text, safe="-._~")


def _pool_node_id(pool_name: Any) -> str:
    return f"pool:{_storage_node_id_component(pool_name)}"


def _vdev_node_id(vdev_name: Any, pool_name: Any = None) -> str:
    vdev_component = _storage_node_id_component(vdev_name)
    if pool_name:
        return f"vdev:{_storage_node_id_component(pool_name)}/{vdev_component}"
    return f"vdev:{vdev_component}"


def storage_node_legacy_alias_ids(object_id: str, object_kind: str | None) -> list[str]:
    kind = normalize_text(object_kind)
    prefix = f"{kind}:" if kind in {"pool", "vdev"} else None
    if not prefix or not object_id.startswith(prefix):
        return []
    component = object_id.removeprefix(prefix)
    if kind == "vdev" and "/" in component:
        component = component.rsplit("/", 1)[1]
    raw_label = unquote(component)
    return _dedupe_strings(
        [
            f"{kind}:{raw_label}",
            f"{kind}:{_object_id_token(raw_label)}",
        ]
    )


def _linux_ses_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", normalize_text(value))
    return (int(match.group(1)) if match else 1_000_000, normalize_text(value))


def _slot_has_disk(slot: SlotView) -> bool:
    if slot.device_name or slot.serial or slot.model or slot.pool_name or slot.vdev_name:
        return True
    if slot.multipath and (slot.multipath.device_name or slot.multipath.members):
        return True
    return False


def _bay_label(slot: SlotView) -> str:
    """The bay number the chassis prints, as the enclosure view shows it.

    A profile with slot_number_base (the UniFi NVRs, #1068) labels slot index 0
    "01"; the graph uses that label so a bay reads the same on every tab.
    """
    label = str(slot.slot_label or "").strip()
    return label if label.isdigit() else f"{slot.slot:02d}"


def _backplane_zones_for_slots(slots: list[SlotView]) -> dict[int, dict[str, Any]]:
    labels = {slot.slot: _bay_label(slot) for slot in slots if isinstance(slot.slot, int)}
    slot_numbers = _snapshot_slot_numbers(slots)
    if not slot_numbers:
        return {}
    slot_count = max(slot_numbers) + 1
    zone_count = 4 if slot_count >= 4 else 1
    zone_size = max(1, (slot_count + zone_count - 1) // zone_count)
    zones_by_slot: dict[int, dict[str, Any]] = {}
    for zone_index in range(zone_count):
        start = zone_index * zone_size
        end = min(slot_count - 1, start + zone_size - 1)
        zone_slots = [slot for slot in slot_numbers if start <= slot <= end]
        if not zone_slots:
            continue
        zone = {
            "id": f"backplane:{zone_index}",
            "index": zone_index,
            "label": f"Backplane Zone {zone_index + 1}",
            "slots": zone_slots,
            "range": f"Bays {labels.get(zone_slots[0], f'{zone_slots[0]:02d}')}-"
            f"{labels.get(zone_slots[-1], f'{zone_slots[-1]:02d}')}",
        }
        for slot in zone_slots:
            zones_by_slot[slot] = zone
    return zones_by_slot


def _sas_fabric_alias_map(aliases: list[SasFabricAlias]) -> dict[str, SasFabricAlias]:
    return {alias.object_id: alias for alias in aliases if alias.object_id and alias.label}


def sas_fabric_legacy_alias_targets(nodes: Iterable[SasFabricNode]) -> dict[str, set[str]]:
    """Map historical IDs to all observed owners, never reverse a digest ID."""
    targets: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        legacy_ids = storage_node_legacy_alias_ids(node.id, node.kind)
        legacy_ids.extend(node.raw.get("legacy_alias_ids", []))
        for legacy_id in legacy_ids:
            targets[legacy_id].add(node.id)
    return dict(targets)


def _resolve_sas_fabric_alias_map(
    aliases: dict[str, SasFabricAlias],
    nodes: dict[str, SasFabricNode],
) -> dict[str, SasFabricAlias]:
    legacy_targets = sas_fabric_legacy_alias_targets(
        node for node in nodes.values() if node.kind in {"pool", "vdev"}
    )

    resolved: dict[str, SasFabricAlias] = {
        object_id: alias
        for object_id, alias in aliases.items()
        if object_id in nodes
        and (
            nodes[object_id].kind not in {"pool", "vdev"}
            or alias.source == SAS_FABRIC_CANONICAL_ALIAS_SOURCE
            or len(legacy_targets.get(object_id, set())) <= 1
        )
    }
    for object_id, alias in aliases.items():
        if object_id in resolved or alias.source == SAS_FABRIC_CANONICAL_ALIAS_SOURCE:
            continue
        targets = legacy_targets.get(object_id, set())
        if len(targets) == 1:
            canonical_id = next(iter(targets))
            existing = resolved.get(canonical_id)
            if existing is None or (
                existing.source != SAS_FABRIC_CANONICAL_ALIAS_SOURCE
                and alias.updated_at > existing.updated_at
            ):
                resolved[canonical_id] = alias.model_copy(update={"object_id": canonical_id})
        elif object_id not in nodes:
            resolved[object_id] = alias
    return resolved


def _resolve_platform_route_aliases(
    aliases: dict[str, SasFabricAlias],
    nodes: dict[str, SasFabricNode],
    warnings: list[str],
) -> dict[str, SasFabricAlias]:
    """Read compatibility only: never rewrite or delete persisted aliases.

    A legacy label-derived ID may name several current routes. Retain that
    alias and warn rather than assigning an operator name to an arbitrary bay.
    Explicit aliases on the new ID take precedence within the same scope;
    enclosure overrides still win over system defaults across ID formats.
    """
    targets = sas_fabric_legacy_alias_targets(
        node for node in nodes.values() if node.raw.get("legacy_alias_ids")
    )
    resolved = dict(aliases)
    candidates: dict[str, list[SasFabricAlias]] = defaultdict(list)
    for legacy_id, node_ids in sorted(targets.items()):
        alias = aliases.get(legacy_id)
        if alias is None or alias.source == SAS_FABRIC_CANONICAL_ALIAS_SOURCE:
            continue
        if len(node_ids) == 1:
            candidates[next(iter(node_ids))].append(alias)
        else:
            warnings.append(
                f"A saved {alias.object_kind or 'route'} alias is ambiguous across {len(node_ids)} "
                "Storage Fabric objects. It was retained but not applied; its ownership must be resolved before renaming."
            )
    for canonical_id, compatible in candidates.items():
        if canonical_id in aliases:
            compatible.append(aliases[canonical_id])
        # Resolve logical scope before ID format. Within that scope, exact
        # canonical IDs win; legacy ties use timestamp then original ID.
        alias = max(compatible, key=lambda item: (
            item.enclosure_id is not None, item.object_id == canonical_id,
            item.updated_at, item.object_id,
        ))
        resolved[canonical_id] = alias.model_copy(update={"object_id": canonical_id})
    return resolved


def _apply_sas_fabric_aliases(
    *,
    nodes: dict[str, SasFabricNode],
    traces: dict[str, SasFabricTrace],
    controllers: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    aliases: dict[str, SasFabricAlias],
) -> None:
    for node in nodes.values():
        alias = aliases.get(node.id)
        if not alias:
            continue
        node.alias = alias.label
        node.display_label = alias.label
        node.raw["operator_alias"] = alias.label

    for trace in traces.values():
        alias = aliases.get(trace.id)
        if not alias:
            continue
        trace.alias = alias.label
        trace.display_label = alias.label
        trace.metrics["operator_alias"] = alias.label

    for controller in controllers:
        alias = aliases.get(str(controller.get("id") or ""))
        if alias:
            controller["alias"] = alias.label
            controller["display_label"] = alias.label

    for path in paths:
        alias = aliases.get(str(path.get("id") or ""))
        if alias:
            path["alias"] = alias.label
            path["display_label"] = alias.label


def _node_raw_for_payload(node: SasFabricNode) -> dict[str, Any]:
    raw = dict(node.raw or {})
    raw.setdefault("id", node.id)
    raw.setdefault("kind", node.kind)
    raw.setdefault("label", node.label)
    raw.setdefault("display_label", node.display_label)
    raw.setdefault("alias", node.alias)
    raw.setdefault("raw_id", node.raw_id)
    raw["related_slots"] = list(node.related_slots)
    raw["status"] = node.status
    return raw


def _controller_status(counts: dict[str, int]) -> str | None:
    if counts.get("fail"):
        return "degraded"
    if counts.get("active") or counts.get("passive"):
        return "online"
    return None


def _path_state_sort_key(state: str) -> int:
    return {"fail": 0, "missing": 1, "active": 2, "passive": 3, "unknown": 4}.get(state, 10)


def _link_id(source: str, target: str, kind: str, slot: int | None = None) -> str:
    suffix = f":{slot}" if slot is not None else ""
    return f"{kind}:{source}->{target}{suffix}"


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def _dedupe_ints(values: Any) -> list[int]:
    seen: set[int] = set()
    deduped: list[int] = []
    for value in values:
        try:
            integer = int(value)
        except (TypeError, ValueError):
            continue
        if integer in seen:
            continue
        seen.add(integer)
        deduped.append(integer)
    return sorted(deduped)
