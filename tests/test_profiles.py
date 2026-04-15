from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import SystemConfig, TrueNASConfig, _load_profile_yaml, get_settings
from app.models.domain import EnclosureOption
from app.services.profile_registry import (
    CORE_CSE_946_PROFILE_ID,
    LINUX_GPU_SERVER_NVME_PROFILE_ID,
    QUANTASTOR_SSG_SHARED_24_PROFILE_ID,
    SCALE_SSG_FRONT_24_PROFILE_ID,
    SCALE_SSG_REAR_12_PROFILE_ID,
    ProfileRegistry,
)


class ProfileRegistryTests(unittest.TestCase):
    def test_builtin_core_profile_preserves_top_loader_grouping(self) -> None:
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
        self.assertEqual(front.slot_layout[0], [5, 11, 17, 23])
        self.assertEqual(front.slot_layout[-1], [0, 6, 12, 18])

        self.assertIsNotNone(rear)
        self.assertEqual(rear.id, SCALE_SSG_REAR_12_PROFILE_ID)
        self.assertEqual(rear.latch_edge, "right")
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
        self.assertEqual(profile.rows, 1)
        self.assertEqual(profile.columns, 24)
        self.assertEqual(profile.slot_layout, [list(range(24))])

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
