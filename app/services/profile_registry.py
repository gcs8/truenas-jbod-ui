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
GENERIC_FRONT_24_1X24_PROFILE_ID = "generic-front-24-1x24"
GENERIC_FRONT_12_3X4_PROFILE_ID = "generic-front-12-3x4"
GENERIC_TOP_60_4X15_PROFILE_ID = "generic-top-60-4x15"
GENERIC_FRONT_60_5X12_PROFILE_ID = "generic-front-60-5x12"
GENERIC_FRONT_84_6X14_PROFILE_ID = "generic-front-84-6x14"
GENERIC_FRONT_102_8X14_PROFILE_ID = "generic-front-102-8x14"
GENERIC_FRONT_106_8X14_PROFILE_ID = "generic-front-106-8x14"

def sparse_slot_layout(
    rows: int,
    columns: int,
    *,
    starting_slot: int = 0,
    excluded_cells: set[tuple[int, int]] | None = None,
) -> list[list[int | None]]:
    layout_rows: list[list[int | None]] = [[None for _ in range(columns)] for _ in range(rows)]
    next_slot = starting_slot
    excluded = excluded_cells or set()
    for row_index in reversed(range(rows)):
        for column_index in range(columns):
            if (row_index, column_index) in excluded:
                continue
            layout_rows[row_index][column_index] = next_slot
            next_slot += 1
    return layout_rows


def merge_slot_layout_sections(*sections: list[list[int | None]]) -> list[list[int | None]]:
    if not sections:
        return []
    row_count = len(sections[0])
    merged: list[list[int | None]] = []
    for row_index in range(row_count):
        row: list[int | None] = []
        for section in sections:
            row.extend(section[row_index])
        merged.append(row)
    return merged


def default_slot_layout(rows: int, columns: int, slot_count: int) -> list[list[int | None]]:
    full_layout = sparse_slot_layout(rows, columns)
    return [
        [slot for slot in row if isinstance(slot, int) and slot < slot_count]
        for row in full_layout
    ]


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
        EnclosureProfileConfig(
            id=GENERIC_FRONT_24_1X24_PROFILE_ID,
            label="Generic Front 24",
            eyebrow="Generic / 1 x 24 Front View",
            summary="Reusable 1-by-24 front-drive profile for common 24-bay SFF and NVMe chassis.",
            panel_title="Front 24 Bay",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="top",
            bay_size="2.5",
            rows=1,
            columns=24,
            slot_layout=default_slot_layout(1, 24, 24),
        ),
        EnclosureProfileConfig(
            id=GENERIC_FRONT_12_3X4_PROFILE_ID,
            label="Generic Front 12",
            eyebrow="Generic / 3 x 4 Front View",
            summary="Reusable 3-by-4 front-drive profile for common 12-bay LFF chassis and JBOD faces.",
            panel_title="Front 12 Bay",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="right",
            bay_size="3.5",
            rows=3,
            columns=4,
            slot_layout=default_slot_layout(3, 4, 12),
        ),
        EnclosureProfileConfig(
            id=GENERIC_TOP_60_4X15_PROFILE_ID,
            label="Generic Top 60",
            eyebrow="Generic / 4 x 15 Top View",
            summary="Reusable 4-by-15 top-loading profile for common 60-bay chassis with a full top face.",
            panel_title="Top 60 Bay",
            edge_label="System front / latch edge",
            face_style="top-loader",
            latch_edge="bottom",
            bay_size="3.5",
            rows=4,
            columns=15,
            slot_layout=default_slot_layout(4, 15, 60),
        ),
        EnclosureProfileConfig(
            id=GENERIC_FRONT_60_5X12_PROFILE_ID,
            label="Generic Front 60",
            eyebrow="Generic / 5 x 12 Front View",
            summary="Reusable 5-by-12 front-drive profile for common 60-bay front-loading shelves.",
            panel_title="Front 60 Bay",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="top",
            bay_size="3.5",
            rows=5,
            columns=12,
            slot_layout=default_slot_layout(5, 12, 60),
        ),
        EnclosureProfileConfig(
            id=GENERIC_FRONT_84_6X14_PROFILE_ID,
            label="Generic Front 84",
            eyebrow="Generic / 6 x 14 Front View",
            summary="Reusable 6-by-14 front-drive profile for common 84-bay dense shelves.",
            panel_title="Front 84 Bay",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="top",
            bay_size="3.5",
            rows=6,
            columns=14,
            slot_layout=default_slot_layout(6, 14, 84),
        ),
        EnclosureProfileConfig(
            id=GENERIC_FRONT_102_8X14_PROFILE_ID,
            label="Generic Front 102",
            eyebrow="Generic / 8 x 14 Front View",
            summary="Reusable 8-by-14 front-drive profile for 102-bay shelves with an internal center beam or airflow gap.",
            panel_title="Front 102 Bay",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="top",
            bay_size="3.5",
            rows=8,
            columns=14,
            slot_layout=sparse_slot_layout(
                8,
                14,
                excluded_cells={
                    (0, 6), (0, 7),
                    (1, 6), (1, 7),
                    (2, 6), (2, 7),
                    (3, 6), (3, 7),
                    (4, 6), (4, 7),
                },
            ),
        ),
        EnclosureProfileConfig(
            id=GENERIC_FRONT_106_8X14_PROFILE_ID,
            label="Generic Front 106",
            eyebrow="Generic / 8 x 14 Front View",
            summary="Reusable 8-by-14 front-drive profile for 106-bay shelves with a 96-drive main field and a 10-drive sidecar section.",
            panel_title="Front 106 Bay",
            edge_label="Front of chassis",
            face_style="front-drive",
            latch_edge="top",
            bay_size="3.5",
            rows=8,
            columns=14,
            slot_layout=merge_slot_layout_sections(
                sparse_slot_layout(
                    8,
                    2,
                    starting_slot=96,
                    excluded_cells={
                        (0, 0), (0, 1),
                        (1, 0), (1, 1),
                        (2, 0), (2, 1),
                    },
                ),
                sparse_slot_layout(8, 12),
            ),
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


def built_in_profile_ids() -> set[str]:
    return {profile.id for profile in _built_in_profiles()}


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

    def select_profile_id(
        self,
        system: SystemConfig,
        enclosure: EnclosureOption | None = None,
    ) -> str | None:
        return self._select_profile_id(system, enclosure)

    def resolve_for_enclosure(
        self,
        system: SystemConfig,
        enclosure: EnclosureOption | None,
        *,
        fallback_label: str | None = None,
        fallback_rows: int | None = None,
        fallback_columns: int | None = None,
        fallback_slot_count: int | None = None,
        fallback_slot_layout: list[list[int | None]] | None = None,
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
