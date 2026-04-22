from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import StorageViewBindingConfig, StorageViewConfig, StorageViewRenderConfig, SystemConfig
from app.services.profile_registry import (
    UNIFI_UNVR_FRONT_4_PROFILE_ID,
    UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
)
from app.services.storage_view_templates import build_sequential_layout, get_storage_view_template

if TYPE_CHECKING:
    from app.models.domain import EnclosureProfileView
    from app.services.profile_registry import ProfileRegistry


UNIFI_EMBEDDED_BOOT_MEDIA_PROFILE_IDS = {
    UNIFI_UNVR_FRONT_4_PROFILE_ID,
    UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
}


def _build_inferred_unifi_boot_media_view() -> StorageViewConfig:
    return StorageViewConfig(
        id="embedded-boot-media",
        label="Embedded Boot Media",
        kind="boot_devices",
        template_id="embedded-boot-media-1",
        enabled=True,
        order=30,
        render=StorageViewRenderConfig(
            show_in_main_ui=True,
            show_in_admin_ui=True,
            default_collapsed=True,
        ),
        binding=StorageViewBindingConfig(
            mode="auto",
            device_names=["boot"],
        ),
    )


def resolve_system_storage_views(
    system: SystemConfig,
    profile_registry: "ProfileRegistry | None" = None,
) -> list[StorageViewConfig]:
    stored_views = list(system.storage_views)
    inferred_profile_id = profile_registry.select_profile_id(system) if profile_registry else None
    if not stored_views and inferred_profile_id:
        stored_views = [
            StorageViewConfig(
                id="primary-chassis",
                label="Primary Chassis",
                kind="ses_enclosure",
                template_id="ses-auto",
                profile_id=inferred_profile_id,
                enabled=True,
                order=10,
                render=StorageViewRenderConfig(
                    show_in_main_ui=True,
                    show_in_admin_ui=True,
                    default_collapsed=False,
                ),
                binding=StorageViewBindingConfig(mode="auto"),
            )
        ]
        if inferred_profile_id in UNIFI_EMBEDDED_BOOT_MEDIA_PROFILE_IDS:
            stored_views.append(_build_inferred_unifi_boot_media_view())

    return sorted(
        stored_views,
        key=lambda item: (item.order, item.label.lower(), item.id),
    )


def resolve_storage_view_profile(
    storage_view: StorageViewConfig,
    *,
    profile_registry: "ProfileRegistry | None" = None,
    selected_profile: "EnclosureProfileView | None" = None,
) -> "EnclosureProfileView | None":
    if storage_view.kind != "ses_enclosure":
        return None
    if profile_registry and storage_view.profile_id:
        resolved = profile_registry.get(storage_view.profile_id)
        if resolved is not None:
            return resolved
    return selected_profile


def build_storage_view_rows(
    storage_view: StorageViewConfig,
    *,
    selected_profile: "EnclosureProfileView | None" = None,
) -> list[list[int | None]]:
    template = get_storage_view_template(storage_view.template_id)
    if storage_view.kind == "ses_enclosure" and selected_profile and selected_profile.slot_layout:
        return selected_profile.slot_layout
    if template and template.slot_layout:
        return template.slot_layout
    return build_sequential_layout(
        template.rows if template else 1,
        template.columns if template else 1,
        template.slot_count if template else 1,
    )


def ordered_storage_view_slot_indices(
    storage_view: StorageViewConfig,
    *,
    selected_profile: "EnclosureProfileView | None" = None,
) -> list[int]:
    layout_rows = build_storage_view_rows(storage_view, selected_profile=selected_profile)
    visible_slots = [
        int(slot_value)
        for row in layout_rows
        for slot_value in row
        if isinstance(slot_value, int)
    ]
    if storage_view.kind == "ses_enclosure":
        return visible_slots
    return sorted(set(visible_slots))


def storage_view_slot_label(
    storage_view: StorageViewConfig,
    slot_value: int,
    *,
    selected_profile: "EnclosureProfileView | None" = None,
) -> str:
    template = get_storage_view_template(storage_view.template_id)
    overrides = storage_view.layout_overrides.slot_labels if storage_view.layout_overrides else {}
    if slot_value in overrides:
        return overrides[slot_value]
    if template and slot_value in template.default_slot_labels:
        return template.default_slot_labels[slot_value]
    if storage_view.kind == "ses_enclosure" and selected_profile:
        return str(slot_value).zfill(2)
    return f"Slot {slot_value + 1}"


def storage_view_slot_size(
    storage_view: StorageViewConfig,
    slot_value: int,
) -> str | None:
    overrides = storage_view.layout_overrides.slot_sizes if storage_view.layout_overrides else {}
    return overrides.get(slot_value)
