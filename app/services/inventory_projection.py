"""Projection leaves of the inventory service.

Three stateless functions that ``InventoryService`` forwards to: the mapping
revision tokens attached to each slot, the platform capability cards of a
snapshot, and the layout frame a correlator draws on. The service keeps a
method of the same name, signature and kind for each one.

This module does not import ``app.services.inventory``. Every argument after
the original parameters is supplied by the forwarding method on each call:

- a value the code read from the service (``self.system``, ``self.settings``,
  ``self.mapping_store``, ``self.profile_registry``) arrives as a getter with no
  arguments, and a service method (``self._slot_disk_binding`` and the like)
  arrives as a getter that returns it. Each getter is called where the method
  used to read the attribute, so a read happens at the same point, only on the
  same paths, and the object read is the one on the instance or the class at
  that moment;
- a name the code read from the inventory module's namespace arrives under
  the same name, taken from that namespace when the forwarding method runs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.config import Settings, SystemConfig
    from app.models.domain import (
        EnclosureOption,
        InventorySummary,
        ManualMapping,
        PlatformCapability as _PlatformCapability,
        SlotView,
        SourceStatus,
    )
    from app.services.mapping_store import MappingStore
    from app.services.profile_registry import ProfileRegistry


def attach_mapping_revisions(
    slots: list[SlotView],
    *,
    loaded_entries=None,
    mapping_store: Callable[[], MappingStore],
    system: Callable[[], SystemConfig],
    slot_disk_binding: Callable[[], Callable[[SlotView | None], str]],
    resolve_physical_mapping_scope: Callable[[str | None], str | None],
    bind_mapping_revision: Callable[[str, str | None], str],
) -> list[SlotView]:
    targets = list({
        (resolve_physical_mapping_scope(slot.enclosure_id), slot.slot)
        for slot in slots
    })
    save_revisions = mapping_store().save_revisions(system().id, targets, loaded_entries=loaded_entries)
    clear_revisions = mapping_store().clear_revisions(
        system().id,
        targets,
        loaded_entries=loaded_entries,
    )
    # Each bay's revisions also bind the disk in it, so an editor opened
    # on one disk cannot save or clear after another took its place (#1141).
    return [
        slot.model_copy(
            update={
                "mapping_revision": bind_mapping_revision(
                    save_revisions[(resolve_physical_mapping_scope(slot.enclosure_id), slot.slot)],
                    slot_disk_binding()(slot),
                ),
                "mapping_clear_revision": bind_mapping_revision(
                    clear_revisions[(resolve_physical_mapping_scope(slot.enclosure_id), slot.slot)],
                    slot_disk_binding()(slot),
                ),
            }
        )
        for slot in slots
    ]


def build_platform_capabilities(
    *,
    slots: list[SlotView],
    available_enclosures: list[EnclosureOption],
    summary: InventorySummary,
    sources: dict[str, SourceStatus],
    platform_context: dict[str, Any],
    selected_profile: Any | None = None,
    system: Callable[[], SystemConfig],
    PlatformCapability: Callable[..., _PlatformCapability],
) -> dict[str, _PlatformCapability]:
    platform = (system().truenas.platform or "core").lower()
    api_status = sources.get("api")
    ssh_status = sources.get("ssh")
    bmc_status = sources.get("bmc")
    ssh_ok = bool(ssh_status and ssh_status.enabled and ssh_status.ok)
    bmc_ok = bool(bmc_status and bmc_status.enabled and bmc_status.ok)
    ssh_enabled = bool(system().ssh.enabled)
    bmc_enabled = bool(system().bmc.enabled)
    disk_or_slot_count = max(summary.disk_count, summary.mapped_slot_count)
    has_enclosures = bool(
        any(option.kind == "physical" for option in available_enclosures)
        or summary.enclosure_count
    )
    has_mapped_slots = summary.mapped_slot_count > 0
    has_profile = selected_profile is not None
    has_led_slots = any(slot.led_supported for slot in slots)
    has_platform_context = bool(platform_context)
    has_linux_ses_slots = any(
        slot.ssh_ses_device
        or slot.ssh_ses_targets
        or (
            isinstance(slot.raw_status, dict)
            and (slot.raw_status.get("ses_device") or slot.raw_status.get("ssh_ses_device"))
        )
        for slot in slots
    )

    def cap(
        label: str,
        status: Literal["available", "partial", "unavailable", "unsupported"],
        summary_text: str,
        *,
        cap_sources: list[str] | None = None,
        requirements: list[str] | None = None,
    ) -> PlatformCapability:
        return PlatformCapability(
            label=label,
            status=status,
            summary=summary_text,
            sources=cap_sources or [],
            requirements=requirements or [],
        )

    inventory_sources_by_platform = {
        "core": ["TrueNAS middleware", "FreeBSD SSH"],
        "scale": ["TrueNAS middleware", "Linux SSH"],
        "quantastor": ["Quantastor REST", "optional Quantastor SSH"],
        "linux": ["Linux SSH"],
        "esxi": ["ESXi SSH", "ESXCLI", "vendor storage CLI"],
        "ipmi": ["BMC / IPMI"],
    }
    inventory_sources = inventory_sources_by_platform.get(platform, ["configured sources"])
    if disk_or_slot_count or has_enclosures:
        inventory_status = "available"
        inventory_summary = "Inventory data is available from the configured platform sources."
    elif (api_status and api_status.enabled and not api_status.ok) or (ssh_status and ssh_status.enabled and not ssh_status.ok):
        inventory_status = "unavailable"
        inventory_summary = "Configured inventory sources did not return usable storage data for this refresh."
    else:
        inventory_status = "partial"
        inventory_summary = "Inventory collection ran, but no disks or enclosures were mapped in this snapshot."

    if platform in {"core", "scale", "quantastor"}:
        if has_enclosures and has_mapped_slots:
            physical_status = "available"
            physical_summary = "Physical slot mapping is available for the selected view."
        elif has_enclosures or has_mapped_slots or summary.ssh_slot_hint_count:
            physical_status = "partial"
            physical_summary = "Some physical slot evidence is available, but this view is not fully mapped."
        else:
            physical_status = "unavailable"
            physical_summary = "No usable enclosure or SES slot evidence is available in this snapshot."
    elif platform in {"linux", "esxi", "ipmi"}:
        if has_profile and has_mapped_slots:
            physical_status = "available"
            physical_summary = "Profile-backed physical slot mapping is available for this host."
        elif has_profile or has_mapped_slots:
            physical_status = "partial"
            physical_summary = "A profile or host-side slot hints are available, but the physical view is incomplete."
        else:
            physical_status = "partial"
            physical_summary = "Physical slots depend on a profile, storage view, BMC data, vendor data, or SES evidence."
    else:
        physical_status = "unavailable"
        physical_summary = "Physical slot capability is unknown for this platform."

    if platform == "core":
        smart_status = "available" if disk_or_slot_count else "partial"
        smart_summary = "SMART detail can use TrueNAS middleware and SSH smartctl enrichment."
        smart_sources = ["TrueNAS middleware", "smartctl"]
        smart_requirements = ["optional SSH smartctl for richer detail"]
    elif platform in {"scale", "linux"}:
        smart_status = "available" if has_mapped_slots and ssh_enabled else "partial"
        smart_summary = "SMART detail is SSH-first on this platform."
        smart_sources = ["smartctl", "optional nvme-cli"]
        smart_requirements = ["smartmontools", "command-limited smartctl access"]
    elif platform == "quantastor":
        smart_status = "available" if disk_or_slot_count else "partial"
        smart_summary = "SMART detail combines Quantastor disk fields with optional SSH smartctl enrichment."
        smart_sources = ["Quantastor REST", "smartctl"]
        smart_requirements = ["optional SSH smartctl on a node that can see the disk"]
    elif platform == "esxi":
        smart_status = "available" if has_mapped_slots and ssh_enabled else "partial"
        smart_summary = "SMART detail is read-only through ESXCLI plus vendor controller health."
        smart_sources = ["ESXCLI", "StorCLI or compatible vendor CLI"]
        smart_requirements = ["ESXi SSH", "vendor storage CLI for physical member detail"]
    elif platform == "ipmi":
        smart_status = "unsupported"
        smart_summary = "BMC-only inventory does not expose host SMART detail."
        smart_sources = ["BMC / IPMI"]
        smart_requirements = ["host SSH or platform API for SMART detail"]
    else:
        smart_status = "unavailable"
        smart_summary = "SMART detail capability is unknown for this platform."
        smart_sources = []
        smart_requirements = []

    if has_mapped_slots:
        history_status = "available"
        history_summary = "History can track mapped slots or storage-view slots with stable identity."
    elif disk_or_slot_count:
        history_status = "partial"
        history_summary = "History can track logical disks, but physical slot history may be limited."
    else:
        history_status = "partial"
        history_summary = "History depends on stable disk or slot identity from inventory."

    if has_led_slots:
        identify_status = "available"
        identify_summary = "At least one slot currently exposes identify LED control."
        identify_sources = ["slot metadata"]
        identify_requirements: list[str] = []
    elif platform == "esxi":
        identify_status = "unsupported"
        identify_summary = "Bay lights cannot be controlled on ESXi hosts."
        identify_sources = ["ESXi SSH"]
        identify_requirements = ["validated vendor or BMC per-slot identify support"]
    elif platform == "ipmi":
        identify_status = "partial" if bmc_enabled else "unsupported"
        identify_summary = "Identify support depends on BMC drive metadata and locator support."
        identify_sources = ["BMC / IPMI"]
        identify_requirements = ["BMC drive inventory with locator support"]
    elif platform in {"scale", "linux", "quantastor"}:
        identify_status = "partial"
        identify_summary = "Identify support depends on SES/BMC/vendor evidence and safe command permissions."
        identify_sources = ["sg_ses", "BMC / IPMI"]
        identify_requirements = ["validated sg_ses identify commands or BMC locator support"]
    elif platform == "core":
        identify_status = "partial"
        identify_summary = "Identify support depends on CORE sesutil locate permissions for the selected enclosure."
        identify_sources = ["sesutil"]
        identify_requirements = ["sesutil locate sudo permissions"]
    else:
        identify_status = "unavailable"
        identify_summary = "Identify capability is unknown for this platform."
        identify_sources = []
        identify_requirements = []

    if has_platform_context:
        platform_details_status = "available"
        platform_details_summary = "Platform-specific context is available for this snapshot."
    elif platform in {"quantastor", "esxi", "ipmi"}:
        platform_details_status = "partial"
        platform_details_summary = "This platform has a dedicated context panel, but no context was available in this snapshot."
    elif bmc_ok:
        platform_details_status = "available"
        platform_details_summary = "BMC enrichment context is available for this snapshot."
    else:
        platform_details_status = "partial"
        platform_details_summary = "Only the standard slot/detail context is available for this platform."

    if platform == "core":
        if ssh_enabled:
            diagnostics_status = "available" if ssh_ok or has_mapped_slots else "partial"
            diagnostics_summary = "CORE SAS Fabric diagnostics can use read-only mprutil and kernel event evidence."
        else:
            diagnostics_status = "partial"
            diagnostics_summary = "CORE SAS Fabric diagnostics require SSH mprutil evidence."
        diagnostics_sources = ["mprutil", "pciconf", "dmidecode", "kernel events"]
        diagnostics_requirements = ["CORE SSH commands for read-only SAS Fabric evidence"]
    elif platform in {"scale", "linux"} and has_linux_ses_slots:
        diagnostics_status = "partial"
        diagnostics_summary = (
            "Linux SES fabric mapping can show SG enclosure paths and bay mapping; "
            "HBA, expander, and kernel-event diagnostics remain CORE-specific."
        )
        diagnostics_sources = [
            "lsblk --json",
            "lsscsi -g -t",
            "nvme list-subsys",
            "sg_ses AES/EC",
            "sg_ses --join --filter",
            "inventory slot metadata",
        ]
        diagnostics_requirements = ["sg3_utils / sg_ses access for observed SG enclosure devices"]
    else:
        diagnostics_status = "unsupported"
        diagnostics_summary = "Deep SAS Fabric diagnostics are currently CORE-specific; this platform keeps transport clues in platform details and debug evidence."
        diagnostics_sources = inventory_sources
        diagnostics_requirements = ["future platform-specific diagnostic source before adding a dedicated surface"]

    return {
        "inventory": cap(
            "Inventory",
            inventory_status,
            inventory_summary,
            cap_sources=inventory_sources,
        ),
        "physical_slots": cap(
            "Physical Slots",
            physical_status,
            physical_summary,
            cap_sources=inventory_sources,
            requirements=["SES, BMC, vendor data, manual mapping, or a profile with stable slot hints"],
        ),
        "smart_detail": cap(
            "SMART Detail",
            smart_status,
            smart_summary,
            cap_sources=smart_sources,
            requirements=smart_requirements,
        ),
        "history": cap(
            "History",
            history_status,
            history_summary,
            cap_sources=["inventory identity", "history sidecar"],
            requirements=["stable disk, slot, or storage-view identity"],
        ),
        "identify": cap(
            "Identify LEDs",
            identify_status,
            identify_summary,
            cap_sources=identify_sources,
            requirements=identify_requirements,
        ),
        "platform_details": cap(
            "Platform Details",
            platform_details_status,
            platform_details_summary,
            cap_sources=inventory_sources + (["BMC / IPMI"] if bmc_ok and "BMC / IPMI" not in inventory_sources else []),
        ),
        "diagnostics": cap(
            "Diagnostics",
            diagnostics_status,
            diagnostics_summary,
            cap_sources=diagnostics_sources,
            requirements=diagnostics_requirements,
        ),
    }


def resolve_layout_frame(
    available_enclosures: list[EnclosureOption],
    selected_enclosure_id: str | None,
    warnings: list[str],
    *,
    selected_meta: dict[str, Any] | None = None,
    selection_warning: str | None = None,
    profile_warning: str | None = None,
    require_selected_option: bool = True,
    require_profile: bool = True,
    settings: Callable[[], Settings],
    system: Callable[[], SystemConfig],
    profile_registry: Callable[[], ProfileRegistry],
    legacy_mapping_fallback_allowed: Callable[[], Callable[[list[EnclosureOption]], bool]],
    resolve_selected_enclosure_option: Callable[[], Callable[..., EnclosureOption | None]],
    enclosure_option_meta: Callable[[], Callable[[EnclosureOption], dict[str, Any]]],
    correlation_mapping_entries: Callable[[], Callable[[], Mapping[str, ManualMapping]]],
    warn_unapplied_legacy_mappings: Callable[[], Callable[..., None]],
    build_layout_rows: Callable[[int, int, int], list[list[int | None]]],
    copy_layout_rows: Callable[[list[list[int | None]] | None], list[list[int | None]]],
    infer_slot_count_from_layout: Callable[..., int],
    layout_slot_positions: Callable[[list[list[int | None]]], dict[int, tuple[int, int]]],
    resolve_physical_mapping_scope: Callable[[str | None], str | None],
    _LayoutFrame: Callable[..., Any],
) -> Any:
    selection_meta = selected_meta if selected_meta is not None else {}
    allow_legacy_mapping_fallback = legacy_mapping_fallback_allowed()(
        available_enclosures
    )
    selected_option = resolve_selected_enclosure_option()(
        available_enclosures,
        selected_enclosure_id,
        selection_meta,
    )
    resolved_meta = (
        selected_meta
        if selected_meta is not None
        else enclosure_option_meta()(selected_option)
        if selected_option is not None
        else {"id": None, "label": None, "name": None}
    )
    if selected_option is None and require_selected_option:
        if selection_warning:
            warnings.append(selection_warning)
        return [], available_enclosures, resolved_meta, [], 0, 0

    fallback_slot_count = (
        selected_option.slot_count
        if selected_option is not None and selected_option.slot_count
        else settings().layout.slot_count
    )
    selected_profile = profile_registry().resolve_for_enclosure(
        system(),
        selected_option,
        fallback_label=selected_option.label if selected_option else selection_meta.get("label"),
        fallback_rows=selected_option.rows if selected_option and selected_option.rows else settings().layout.rows,
        fallback_columns=selected_option.columns if selected_option and selected_option.columns else settings().layout.columns,
        fallback_slot_count=fallback_slot_count,
        fallback_slot_layout=selected_option.slot_layout if selected_option else None,
    )
    if selected_profile is None and require_profile:
        if profile_warning:
            warnings.append(profile_warning)
        return [], available_enclosures, resolved_meta, [], 0, 0

    layout_columns = (
        selected_profile.columns
        if selected_profile is not None
        else selected_option.columns if selected_option is not None and selected_option.columns
        else settings().layout.columns
    )
    layout_rows = (
        copy_layout_rows(selected_profile.slot_layout)
        if selected_profile is not None and (require_profile or selected_profile.slot_layout)
        else copy_layout_rows(selected_option.slot_layout)
        if selected_option is not None and selected_option.slot_layout
        else build_layout_rows(
            selected_option.rows if selected_option is not None and selected_option.rows else settings().layout.rows,
            layout_columns,
            fallback_slot_count,
        )
    )
    layout_count_fallback = (
        selected_option.slot_count
        if require_profile and selected_option is not None
        else fallback_slot_count
    )
    layout_slot_count = infer_slot_count_from_layout(layout_rows, layout_count_fallback)
    slot_positions = layout_slot_positions(layout_rows)
    reported_slot_count = selected_option.slot_count if selected_option is not None else None
    if (
        selected_profile is not None
        and reported_slot_count
        and layout_slot_count
        and reported_slot_count > layout_slot_count
    ):
        warnings.append(
            f"This enclosure reports {reported_slot_count} bays but the selected layout draws "
            f"{layout_slot_count}, so the extra bays are not shown. Choose a matching layout in System Setup."
        )
    # One immutable classification per correlation pass: the entries are
    # loaded here and read back off the frame by every caller.
    loaded_mappings = correlation_mapping_entries()()
    if not allow_legacy_mapping_fallback:
        warn_unapplied_legacy_mappings()(
            warnings,
            # Drawer sub-views resolve mappings against the base shelf id,
            # so the probe has to use the same key the correlator uses.
            resolve_physical_mapping_scope(resolved_meta.get("id")),
            slot_positions,
            loaded_mappings,
            no_identified_physical_enclosure=not available_enclosures,
        )
    return _LayoutFrame(
        available_enclosures=available_enclosures,
        selected_option=selected_option,
        selected_profile=selected_profile,
        selected_meta=resolved_meta,
        layout_rows=layout_rows,
        layout_slot_count=layout_slot_count,
        layout_columns=layout_columns,
        slot_positions=slot_positions,
        allow_legacy_mapping_fallback=allow_legacy_mapping_fallback,
        loaded_mappings=loaded_mappings,
    )
