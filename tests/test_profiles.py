from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import SystemConfig, TrueNASConfig, _load_profile_yaml, get_settings
from app.models.domain import EnclosureOption
from app.services.profile_registry import (
    CORE_CSE_946_PROFILE_ID,
    ESXI_AOC_SLG4_2H8M2_PROFILE_ID,
    GENERIC_FRONT_12_3X4_PROFILE_ID,
    GENERIC_FRONT_24_1X24_PROFILE_ID,
    GENERIC_FRONT_60_5X12_PROFILE_ID,
    GENERIC_FRONT_84_6X14_PROFILE_ID,
    GENERIC_FRONT_102_8X14_PROFILE_ID,
    GENERIC_FRONT_106_8X14_PROFILE_ID,
    GENERIC_TOP_60_4X15_PROFILE_ID,
    LINUX_GPU_SERVER_NVME_PROFILE_ID,
    QUANTASTOR_SSG_SHARED_24_PROFILE_ID,
    SCALE_SSG_FRONT_24_PROFILE_ID,
    SCALE_SSG_REAR_12_PROFILE_ID,
    ProfileRegistry,
    UNIFI_UNVR_FRONT_4_PROFILE_ID,
    UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
)


class ProfileRegistryTests(unittest.TestCase):
    def test_builtin_core_profile_preserves_top_loader_layout(self) -> None:
        system = SystemConfig(id="archive-core", label="Archive CORE", truenas=TrueNASConfig(platform="core"))
        registry = ProfileRegistry(get_settings())

        profile = registry.resolve_for_enclosure(
            system,
            None,
            fallback_label="Archive Shelf",
            fallback_rows=4,
            fallback_columns=15,
            fallback_slot_count=60,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, CORE_CSE_946_PROFILE_ID)
        self.assertEqual(profile.row_groups, [6, 6, 3])
        self.assertEqual(profile.latch_edge, "bottom")
        self.assertEqual(profile.bay_size, "3.5")
        self.assertEqual(profile.slot_layout[0], list(range(45, 60)))
        self.assertEqual(profile.slot_layout[-1], list(range(0, 15)))

    def test_builtin_scale_profiles_preserve_validated_front_and_rear_ordering(self) -> None:
        system = SystemConfig(id="offsite-scale", label="Offsite SCALE", truenas=TrueNASConfig(platform="scale"))
        registry = ProfileRegistry(get_settings())

        front = registry.resolve_for_enclosure(
            system,
            EnclosureOption(
                id="front",
                label="Front 24 Bay",
                profile_id=SCALE_SSG_FRONT_24_PROFILE_ID,
                rows=6,
                columns=4,
                slot_count=24,
            ),
            fallback_label="Front 24 Bay",
            fallback_rows=6,
            fallback_columns=4,
            fallback_slot_count=24,
        )
        rear = registry.resolve_for_enclosure(
            system,
            EnclosureOption(
                id="rear",
                label="Rear 12 Bay",
                profile_id=SCALE_SSG_REAR_12_PROFILE_ID,
                rows=3,
                columns=4,
                slot_count=12,
            ),
            fallback_label="Rear 12 Bay",
            fallback_rows=3,
            fallback_columns=4,
            fallback_slot_count=12,
        )

        self.assertIsNotNone(front)
        self.assertEqual(front.id, SCALE_SSG_FRONT_24_PROFILE_ID)
        self.assertEqual(front.latch_edge, "right")
        self.assertEqual(front.bay_size, "3.5")
        self.assertEqual(front.slot_layout[0], [5, 11, 17, 23])
        self.assertEqual(front.slot_layout[-1], [0, 6, 12, 18])

        self.assertIsNotNone(rear)
        self.assertEqual(rear.id, SCALE_SSG_REAR_12_PROFILE_ID)
        self.assertEqual(rear.latch_edge, "right")
        self.assertEqual(rear.bay_size, "3.5")
        self.assertEqual(rear.slot_layout, [[2, 5, 8, 11], [1, 4, 7, 10], [0, 3, 6, 9]])

    def test_builtin_linux_gpu_profile_exposes_two_nvme_slot_hints(self) -> None:
        system = SystemConfig(id="gpu-server", label="GPU Server", truenas=TrueNASConfig(platform="linux"))
        registry = ProfileRegistry(get_settings())

        profile = registry.resolve_for_enclosure(
            system,
            None,
            fallback_label="Right NVMe 2",
            fallback_rows=1,
            fallback_columns=2,
            fallback_slot_count=2,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, LINUX_GPU_SERVER_NVME_PROFILE_ID)
        self.assertEqual(profile.latch_edge, "bottom")
        self.assertEqual(profile.bay_size, "2.5")
        self.assertEqual(profile.slot_layout, [[0, 1]])
        self.assertEqual(profile.slot_hints[0], ["nvme0", "10000:01:00.0"])
        self.assertEqual(profile.slot_hints[1], ["nvme1", "10000:02:00.0"])

    def test_builtin_quantastor_profile_exposes_shared_24_slot_layout(self) -> None:
        system = SystemConfig(id="quantastor-lab", label="Quantastor Lab", truenas=TrueNASConfig(platform="quantastor"))
        registry = ProfileRegistry(get_settings())

        profile = registry.resolve_for_enclosure(
            system,
            None,
            fallback_label="Shared Front 24",
            fallback_rows=1,
            fallback_columns=24,
            fallback_slot_count=24,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, QUANTASTOR_SSG_SHARED_24_PROFILE_ID)
        self.assertEqual(profile.latch_edge, "top")
        self.assertEqual(profile.bay_size, "3.5")
        self.assertEqual(profile.rows, 1)
        self.assertEqual(profile.columns, 24)
        self.assertEqual(profile.slot_layout, [list(range(24))])

    def test_builtin_esxi_aoc_profile_exposes_two_m2_slots(self) -> None:
        system = SystemConfig(id="cryo-esxi", label="CryoStorage ESXi", truenas=TrueNASConfig(platform="esxi"))
        registry = ProfileRegistry(get_settings())

        profile = registry.resolve_for_enclosure(
            system,
            None,
            fallback_label="AOC-SLG4-2H8M2",
            fallback_rows=2,
            fallback_columns=1,
            fallback_slot_count=2,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, ESXI_AOC_SLG4_2H8M2_PROFILE_ID)
        self.assertEqual(profile.face_style, "nvme-carrier")
        self.assertEqual(profile.slot_layout, [[1], [0]])
        self.assertEqual(profile.slot_hints[0], ["13:0", "C0 x4", "0(path0)"])
        self.assertEqual(profile.slot_hints[1], ["13:1", "C1 x4", "1(path0)"])

    def test_builtin_generic_profiles_expose_reusable_reference_geometries(self) -> None:
        registry = ProfileRegistry(get_settings())

        expected_profiles = {
            GENERIC_FRONT_24_1X24_PROFILE_ID: {
                "face_style": "front-drive",
                "latch_edge": "top",
                "bay_size": "2.5",
                "rows": 1,
                "columns": 24,
                "top_row": list(range(24)),
                "bottom_row": list(range(24)),
            },
            GENERIC_FRONT_12_3X4_PROFILE_ID: {
                "face_style": "front-drive",
                "latch_edge": "right",
                "bay_size": "3.5",
                "rows": 3,
                "columns": 4,
                "top_row": [8, 9, 10, 11],
                "bottom_row": [0, 1, 2, 3],
            },
            GENERIC_TOP_60_4X15_PROFILE_ID: {
                "face_style": "top-loader",
                "latch_edge": "bottom",
                "bay_size": "3.5",
                "rows": 4,
                "columns": 15,
                "top_row": list(range(45, 60)),
                "bottom_row": list(range(0, 15)),
            },
            GENERIC_FRONT_60_5X12_PROFILE_ID: {
                "face_style": "front-drive",
                "latch_edge": "top",
                "bay_size": "3.5",
                "rows": 5,
                "columns": 12,
                "top_row": list(range(48, 60)),
                "bottom_row": list(range(0, 12)),
            },
            GENERIC_FRONT_84_6X14_PROFILE_ID: {
                "face_style": "front-drive",
                "latch_edge": "top",
                "bay_size": "3.5",
                "rows": 6,
                "columns": 14,
                "top_row": list(range(70, 84)),
                "bottom_row": list(range(0, 14)),
            },
            GENERIC_FRONT_102_8X14_PROFILE_ID: {
                "face_style": "front-drive",
                "latch_edge": "top",
                "bay_size": "3.5",
                "rows": 8,
                "columns": 14,
                "top_row": [90, 91, 92, 93, 94, 95, None, None, 96, 97, 98, 99, 100, 101],
                "bottom_row": list(range(0, 14)),
            },
            GENERIC_FRONT_106_8X14_PROFILE_ID: {
                "face_style": "front-drive",
                "latch_edge": "top",
                "bay_size": "3.5",
                "rows": 8,
                "columns": 14,
                "top_row": [None, None, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95],
                "bottom_row": [96, 97, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
            },
        }

        listed_ids = {profile.id for profile in registry.list_profiles()}
        for profile_id in expected_profiles:
            self.assertIn(profile_id, listed_ids)

        for profile_id, expected in expected_profiles.items():
            with self.subTest(profile_id=profile_id):
                profile = registry.get(profile_id)

                self.assertIsNotNone(profile)
                self.assertEqual(profile.face_style, expected["face_style"])
                self.assertEqual(profile.latch_edge, expected["latch_edge"])
                self.assertEqual(profile.bay_size, expected["bay_size"])
                self.assertEqual(profile.rows, expected["rows"])
                self.assertEqual(profile.columns, expected["columns"])
                self.assertEqual(profile.slot_layout[0], expected["top_row"])
                self.assertEqual(profile.slot_layout[-1], expected["bottom_row"])

        profile_102 = registry.get(GENERIC_FRONT_102_8X14_PROFILE_ID)
        self.assertIsNotNone(profile_102)
        self.assertEqual(profile_102.slot_layout[4][6:8], [None, None])

        profile_106 = registry.get(GENERIC_FRONT_106_8X14_PROFILE_ID)
        self.assertIsNotNone(profile_106)
        self.assertEqual(profile_106.slot_layout[0][:2], [None, None])
        self.assertEqual(profile_106.slot_layout[3][:2], [104, 105])

    def test_builtin_unvr_profile_can_be_selected_explicitly_for_linux_hosts(self) -> None:
        system = SystemConfig(
            id="unvr",
            label="UniFi UNVR",
            default_profile_id=UNIFI_UNVR_FRONT_4_PROFILE_ID,
            truenas=TrueNASConfig(platform="linux"),
        )
        registry = ProfileRegistry(get_settings())

        profile = registry.resolve_for_enclosure(
            system,
            None,
            fallback_label="Front 4 Bay",
            fallback_rows=1,
            fallback_columns=4,
            fallback_slot_count=4,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, UNIFI_UNVR_FRONT_4_PROFILE_ID)
        self.assertEqual(profile.face_style, "unifi-drive")
        self.assertEqual(profile.latch_edge, "bottom")
        self.assertEqual(profile.bay_size, "3.5")
        self.assertEqual(profile.slot_layout, [[0, 1, 2, 3]])
        self.assertEqual(profile.slot_hints[0], ["0:0:0:0"])
        self.assertEqual(profile.slot_hints[3], ["6:0:0:0"])

    def test_builtin_unvr_pro_profile_can_be_selected_explicitly_for_linux_hosts(self) -> None:
        system = SystemConfig(
            id="unvr-pro",
            label="UniFi UNVR Pro",
            default_profile_id=UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
            truenas=TrueNASConfig(platform="linux"),
        )
        registry = ProfileRegistry(get_settings())

        profile = registry.resolve_for_enclosure(
            system,
            None,
            fallback_label="Front 7 Bay",
            fallback_rows=2,
            fallback_columns=4,
            fallback_slot_count=7,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID)
        self.assertEqual(profile.face_style, "unifi-drive")
        self.assertEqual(profile.latch_edge, "bottom")
        self.assertEqual(profile.bay_size, "3.5")
        self.assertEqual(profile.slot_layout, [[0, 1, 2], [3, 4, 5, 6]])
        self.assertEqual(profile.slot_hints[0], ["7:0:0:0"])
        self.assertEqual(profile.slot_hints[1], ["5:0:0:0"])

    def test_get_settings_loads_custom_profile_file_and_system_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            profile_path = temp_root / "profiles.yaml"
            config_path = temp_root / "config.yaml"

            profile_path.write_text(
                "\n".join(
                    [
                        "profiles:",
                        "  - id: custom-lab-front-8",
                        "    label: Custom Lab Front 8",
                        "    eyebrow: Custom LAB / Front View",
                        "    summary: Operator-defined front-drive layout.",
                        "    panel_title: Lab Front 8 Bay",
                        "    edge_label: Front of chassis",
                        "    face_style: front-drive",
                        "    latch_edge: right",
                        "    bay_size: 3.5",
                        "    rows: 2",
                        "    columns: 4",
                        "    slot_layout:",
                        "      - [4, 5, 6, 7]",
                        "      - [0, 1, 2, 3]",
                        "    row_groups: [2, 2]",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "paths:",
                        f"  profile_file: {profile_path.as_posix()}",
                        "systems:",
                        "  - id: custom-lab",
                        "    label: Custom Lab",
                        "    default_profile_id: custom-lab-front-8",
                        "    truenas:",
                        "      platform: core",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"APP_CONFIG_PATH": config_path.as_posix()}, clear=False):
                get_settings.cache_clear()
                settings = get_settings()
                get_settings.cache_clear()

            self.assertEqual(settings.profiles[0].id, "custom-lab-front-8")
            self.assertEqual(settings.systems[0].default_profile_id, "custom-lab-front-8")

            registry = ProfileRegistry(settings)
            profile = registry.resolve_for_enclosure(
                settings.systems[0],
                None,
                fallback_label="Custom Lab Front 8",
                fallback_rows=2,
                fallback_columns=4,
                fallback_slot_count=8,
            )

            self.assertIsNotNone(profile)
            self.assertEqual(profile.id, "custom-lab-front-8")
            self.assertEqual(profile.latch_edge, "right")
            self.assertEqual(profile.bay_size, "3.5")
            self.assertEqual(profile.row_groups, [2, 2])
            self.assertEqual(profile.slot_layout, [[4, 5, 6, 7], [0, 1, 2, 3]])

    def test_profiles_example_yaml_parses_with_loader(self) -> None:
        profile_path = Path(__file__).resolve().parents[1] / "config" / "profiles.example.yaml"

        loaded = _load_profile_yaml(profile_path)

        self.assertIn("profiles", loaded)
        self.assertEqual(loaded["profiles"][0]["id"], "custom-lab-front-8")
        self.assertEqual(loaded["profiles"][1]["id"], "custom-jbod-top-12")
        self.assertEqual(loaded["profiles"][2]["id"], "custom-right-nvme-2")
        self.assertEqual(loaded["profiles"][2]["slot_hints"][0], ["nvme0", "0000:01:00.0"])
