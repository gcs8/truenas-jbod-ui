from __future__ import annotations

from typing import Iterable

from app.config import EnclosureProfileConfig, Settings, SystemConfig, normalize_text
from app.models.domain import EnclosureOption, EnclosureProfileView

CORE_CSE_946_PROFILE_ID = "supermicro-cse-946-top-60"
SCALE_SSG_FRONT_24_PROFILE_ID = "supermicro-ssg-6048r-front-24"
SCALE_SSG_REAR_12_PROFILE_ID = "supermicro-ssg-6048r-rear-12"
LINUX_GPU_SERVER_NVME_PROFILE_ID = "supermicro-sys-2029gp-tr-right-nvme-2"
QUANTASTOR_SSG_SHARED_24_PROFILE_ID = "supermicro-ssg-2028r-shared-front-24"
UNIFI_UNVR_FRONT_4_PROFILE_ID = "ubiquiti-unvr-front-4"
UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID = "ubiquiti-unvr-pro-front-7"


def default_slot_layout(rows: int, columns: int, slot_count: int) -> list[list[int]]:
    layout_rows: list[list[int]] = []
    for row_index in reversed(range(rows)):
        start = row_index * columns
        row_slots = [slot for slot in range(start, start + columns) if slot < slot_count]
        if row_slots:
            layout_rows.append(row_slots)
    return layout_rows


def _built_in_profiles() -> list[EnclosureProfileConfig]:
    return [
        EnclosureProfileConfig(
            id=CORE_CSE_946_PROFILE_ID,
            label="Supermicro CSE-946 Top",
            eyebrow="TrueNAS CORE / Supermicro CSE-946 Top View",
            summary="Top-loading bay map with API-or-SSH LED control and optional SSH enrichment.",
            panel_title="Enclosure Top",
            edge_label="System front / latch edge",
            face_style="top-loader",
            latch_edge="bottom",
            bay_size="3.5",
            rows=4,
            columns=15,
            slot_layout=[
                list(range(45, 60)),
                list(range(30, 45)),
                list(range(15, 30)),
                list(range(0, 15)),
            ],
            row_groups=[6, 6, 3],
        ),
        EnclosureProfileConfig(
            id=SCALE_SSG_FRONT_24_PROFILE_ID,
            label="Supermicro SSG-6048R Front 24",
            eyebrow="TrueNAS SCALE / Supermicro SSG-6048R Front View",
            summary="Front-drive map with Linux SES AES slot mapping and SSH smartctl enrichment.",
            panel_title="Front 24 Bay",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="right",
            bay_size="3.5",
            rows=6,
            columns=4,
            slot_layout=[
                [5, 11, 17, 23],
                [4, 10, 16, 22],
                [3, 9, 15, 21],
                [2, 8, 14, 20],
                [1, 7, 13, 19],
                [0, 6, 12, 18],
            ],
        ),
        EnclosureProfileConfig(
            id=SCALE_SSG_REAR_12_PROFILE_ID,
            label="Supermicro SSG-6048R Rear 12",
            eyebrow="TrueNAS SCALE / Supermicro SSG-6048R Rear View",
            summary="Rear-drive map with Linux SES AES slot mapping and SSH smartctl enrichment.",
            panel_title="Rear 12 Bay",
            edge_label="Rear of chassis",
            face_style="rear-drive",
            latch_edge="right",
            bay_size="3.5",
            rows=3,
            columns=4,
            slot_layout=[
                [2, 5, 8, 11],
                [1, 4, 7, 10],
                [0, 3, 6, 9],
            ],
        ),
        EnclosureProfileConfig(
            id=LINUX_GPU_SERVER_NVME_PROFILE_ID,
            label="Supermicro SYS-2029GP-TR Right NVMe 2",
            eyebrow="Generic Linux / Supermicro SYS-2029GP-TR NVMe View",
            summary="SSH-only Linux profile for the two right-side NVMe bays on a SYS-2029GP-TR host.",
            panel_title="Right NVMe 2",
            edge_label="Rear of chassis",
            face_style="rear-drive",
            latch_edge="bottom",
            bay_size="2.5",
            rows=1,
            columns=2,
            slot_layout=[
                [0, 1],
            ],
            slot_hints={
                0: ["nvme0", "10000:01:00.0"],
                1: ["nvme1", "10000:02:00.0"],
            },
        ),
        EnclosureProfileConfig(
            id=UNIFI_UNVR_FRONT_4_PROFILE_ID,
            label="Ubiquiti UniFi UNVR Front 4",
            eyebrow="Generic Linux / Ubiquiti UniFi UNVR Front View",
            summary="First-pass 4-bay front-drive profile for UniFi UNVR and similar password-SSH Linux appliances.",
            panel_title="Front 4 Bay",
            edge_label="Front of chassis",
            face_style="unifi-drive",
            latch_edge="bottom",
            bay_size="3.5",
            rows=1,
            columns=4,
            slot_layout=[
                [0, 1, 2, 3],
            ],
            slot_hints={
                0: ["0:0:0:0"],
                1: ["2:0:0:0"],
                2: ["4:0:0:0"],
                3: ["6:0:0:0"],
            },
        ),
        EnclosureProfileConfig(
            id=UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
            label="Ubiquiti UniFi UNVR Pro Front 7",
            eyebrow="Generic Linux / Ubiquiti UniFi UNVR Pro Front View",
            summary="First-pass 7-bay front-drive profile for UniFi UNVR Pro appliances, using the validated 3-over-4 physical face layout.",
            panel_title="Front 7 Bay",
            edge_label="Front of chassis",
            face_style="unifi-drive",
            latch_edge="bottom",
            bay_size="3.5",
            rows=2,
            columns=4,
            slot_layout=[
                [0, 1, 2],
                [3, 4, 5, 6],
            ],
            slot_hints={
                # On the validated UNVR Pro test unit, the two installed disks were
                # reported by the UniFi UI as HDD 1 / HDD 2 in bays 1 and 2, while
                # Linux exposed them as HCTL 7:0:0:0 and 5:0:0:0 respectively.
                0: ["7:0:0:0"],
                1: ["5:0:0:0"],
            },
        ),
        EnclosureProfileConfig(
            id=QUANTASTOR_SSG_SHARED_24_PROFILE_ID,
            label="Supermicro SSG-2028R Shared Front 24",
            eyebrow="OSNexus Quantastor / Supermicro SSG-2028R Front View",
            summary="First-pass shared 24-slot front-drive profile for Quantastor dual-node chassis validation.",
            panel_title="Shared Front 24",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="top",
            bay_size="3.5",
            rows=1,
            columns=24,
            slot_layout=[
                list(range(24)),
            ],
        ),
    ]


def _profile_to_view(profile: EnclosureProfileConfig) -> EnclosureProfileView:
    slot_layout = profile.slot_layout or default_slot_layout(profile.rows, profile.columns, profile.rows * profile.columns)
    return EnclosureProfileView(
        id=profile.id,
        label=profile.label,
        eyebrow=profile.eyebrow,
        summary=profile.summary,
        panel_title=profile.panel_title or profile.label,
        edge_label=profile.edge_label,
        face_style=profile.face_style,
        latch_edge=profile.latch_edge,
        bay_size=profile.bay_size,
        rows=profile.rows,
        columns=profile.columns,
        slot_layout=slot_layout,
        row_groups=list(profile.row_groups),
        slot_hints={int(slot): list(hints) for slot, hints in (profile.slot_hints or {}).items()},
    )


class ProfileRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._profiles: dict[str, EnclosureProfileConfig] = {}
        for profile in [*_built_in_profiles(), *settings.profiles]:
            self._profiles[profile.id] = profile

    def list_profiles(self) -> list[EnclosureProfileView]:
        return [_profile_to_view(profile) for profile in self._profiles.values()]

    def get(self, profile_id: str | None) -> EnclosureProfileView | None:
        if not profile_id:
            return None
        profile = self._profiles.get(profile_id)
        return _profile_to_view(profile) if profile else None

    def resolve_for_enclosure(
        self,
        system: SystemConfig,
        enclosure: EnclosureOption | None,
        *,
        fallback_label: str | None = None,
        fallback_rows: int | None = None,
        fallback_columns: int | None = None,
        fallback_slot_count: int | None = None,
        fallback_slot_layout: list[list[int]] | None = None,
    ) -> EnclosureProfileView | None:
        profile_id = self._select_profile_id(system, enclosure)
        resolved = self.get(profile_id)
        if resolved is not None:
            return resolved

        rows = fallback_rows or enclosure.rows if enclosure else None
        columns = fallback_columns or enclosure.columns if enclosure else None
        slot_count = fallback_slot_count or enclosure.slot_count if enclosure else None
        slot_layout = fallback_slot_layout or enclosure.slot_layout if enclosure else None
        label = fallback_label or (enclosure.label if enclosure else None)
        if rows and columns:
            runtime_id = normalize_text(profile_id) or f"runtime-{(enclosure.id if enclosure else 'enclosure')}"
            return EnclosureProfileView(
                id=runtime_id,
                label=label or "Runtime Enclosure",
                eyebrow=system.label,
                summary="Rendered with a runtime enclosure profile inferred from the current geometry.",
                panel_title=label or "Enclosure",
                edge_label="System front",
                face_style="generic",
                latch_edge="bottom",
                bay_size=None,
                rows=rows,
                columns=columns,
                slot_layout=slot_layout or default_slot_layout(rows, columns, slot_count or rows * columns),
                row_groups=[],
            )
        return None

    def _select_profile_id(self, system: SystemConfig, enclosure: EnclosureOption | None) -> str | None:
        if enclosure:
            enclosure_override = (system.enclosure_profiles or {}).get(enclosure.id)
            if enclosure_override:
                return enclosure_override
            if enclosure.profile_id:
                return enclosure.profile_id

        if system.default_profile_id:
            return system.default_profile_id

        if system.truenas.platform == "core":
            return CORE_CSE_946_PROFILE_ID
        if system.truenas.platform == "linux":
            return LINUX_GPU_SERVER_NVME_PROFILE_ID
        if system.truenas.platform == "quantastor":
            return QUANTASTOR_SSG_SHARED_24_PROFILE_ID

        return None


def summarize_row_groups(row_groups: Iterable[int], total_columns: int) -> list[int]:
    normalized = [group for group in row_groups if isinstance(group, int) and group > 0]
    if not normalized or sum(normalized) != total_columns:
        return [total_columns]
    return normalized
