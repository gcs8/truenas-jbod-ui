from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field

from app.config import (
    StorageViewBindingConfig,
    StorageViewKind,
    StorageViewRenderConfig,
)


class StorageViewTemplate(BaseModel):
    id: str
    label: str
    default_id: str
    default_label: str
    kind: StorageViewKind
    summary: str
    rows: int
    columns: int
    slot_count: int
    slot_layout: list[list[int | None]] = Field(default_factory=list)
    default_render: StorageViewRenderConfig = Field(default_factory=StorageViewRenderConfig)
    default_binding: StorageViewBindingConfig = Field(default_factory=StorageViewBindingConfig)
    default_slot_labels: dict[int, str] = Field(default_factory=dict)
    supports_led: bool = False
    supports_auto_discovery: bool = False
    notes: str | None = None


def build_sequential_layout(rows: int, columns: int, slot_count: int) -> list[list[int | None]]:
    layout: list[list[int | None]] = []
    slot_number = 0
    for _ in range(max(1, rows)):
        row: list[int | None] = []
        for _ in range(max(1, columns)):
            if slot_number < slot_count:
                row.append(slot_number)
                slot_number += 1
            else:
                row.append(None)
        layout.append(row)
    return layout


@lru_cache
def list_storage_view_templates() -> list[StorageViewTemplate]:
    return [
        StorageViewTemplate(
            id="ses-auto",
            label="SES Enclosure",
            default_id="front-bays",
            default_label="Front Bays",
            kind="ses_enclosure",
            summary=(
                "Saved chassis overlay for a live SES-backed chassis or shelf. Live discovered "
                "enclosures already auto-populate in the main UI, and this view lets you keep a "
                "profile-backed mirror when you want one."
            ),
            rows=4,
            columns=6,
            slot_count=24,
            slot_layout=build_sequential_layout(4, 6, 24),
            default_render=StorageViewRenderConfig(
                show_in_main_ui=True,
                show_in_admin_ui=True,
                default_collapsed=False,
            ),
            default_binding=StorageViewBindingConfig(mode="auto"),
            supports_led=True,
            supports_auto_discovery=True,
            notes=(
                "Use this when you want a saved chassis view that mirrors a live SES enclosure. "
                "The discovered live enclosure still appears on its own at runtime."
            ),
        ),
        StorageViewTemplate(
            id="nvme-carrier-4",
            label="4x NVMe Carrier Card",
            default_id="nvme-carrier-4",
            default_label="4x NVMe Carrier Card",
            kind="nvme_carrier",
            summary=(
                "Four-slot internal NVMe carrier card for fixed motherboard or PCIe add-in layouts."
            ),
            rows=4,
            columns=1,
            slot_count=4,
            # Render top-to-bottom like the physical card: M2-4 at the top and M2-1 nearest the PCIe edge.
            slot_layout=[[3], [2], [1], [0]],
            default_render=StorageViewRenderConfig(
                show_in_main_ui=True,
                show_in_admin_ui=True,
                default_collapsed=False,
            ),
            default_binding=StorageViewBindingConfig(mode="hybrid"),
            default_slot_labels={
                0: "M2-1",
                1: "M2-2",
                2: "M2-3",
                3: "M2-4",
            },
            supports_led=False,
            supports_auto_discovery=False,
            notes="Good for internal all-flash groups that should stay attached to the same host. Slot 1 is nearest the PCIe edge.",
        ),
        StorageViewTemplate(
            id="satadom-pair-2",
            label="SATADOM Pair",
            default_id="boot-doms",
            default_label="Boot SATADOMs",
            kind="boot_devices",
            summary="Two-slot internal boot-device view for motherboard SATADOM pairs or similar fixed media.",
            rows=1,
            columns=2,
            slot_count=2,
            slot_layout=[[0, 1]],
            default_render=StorageViewRenderConfig(
                show_in_main_ui=False,
                show_in_admin_ui=True,
                default_collapsed=True,
            ),
            default_binding=StorageViewBindingConfig(mode="pool"),
            default_slot_labels={
                0: "DOM-A",
                1: "DOM-B",
            },
            supports_led=False,
            supports_auto_discovery=False,
            notes="Useful for mirrored boot pools that should stay visible for maintenance but not dominate the main read UI.",
        ),
        StorageViewTemplate(
            id="manual-4",
            label="Manual 4-Slot Group",
            default_id="manual-group",
            default_label="Manual Group",
            kind="manual",
            summary="Small manual storage group for odd internal layouts that do not map cleanly to SES.",
            rows=2,
            columns=2,
            slot_count=4,
            slot_layout=[[0, 1], [2, 3]],
            default_render=StorageViewRenderConfig(
                show_in_main_ui=True,
                show_in_admin_ui=True,
                default_collapsed=False,
            ),
            default_binding=StorageViewBindingConfig(mode="serial"),
            supports_led=False,
            supports_auto_discovery=False,
            notes="Start here for internal layouts you want to bind by pool, serial, or device hints later.",
        ),
    ]


@lru_cache
def storage_view_template_index() -> dict[str, StorageViewTemplate]:
    templates = {template.id: template for template in list_storage_view_templates()}
    carrier_template = templates.get("nvme-carrier-4")
    if carrier_template is not None:
        templates["asus-hyper-m2-x16-4"] = carrier_template
    return templates


def get_storage_view_template(template_id: str | None) -> StorageViewTemplate | None:
    if not template_id:
        return None
    return storage_view_template_index().get(template_id)
