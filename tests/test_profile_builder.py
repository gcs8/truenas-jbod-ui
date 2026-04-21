from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import EnclosureProfileConfig, PathConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import EnclosureProfileRequest
from app.services.profile_builder import ProfileBuilderService
from app.services.profile_registry import GENERIC_FRONT_24_1X24_PROFILE_ID


class ProfileBuilderServiceTests(unittest.TestCase):
    def test_save_profile_clones_source_layout_when_geometry_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config.yaml"
            profile_path = temp_root / "profiles.yaml"
            settings = Settings(
                config_file=str(config_path),
                paths=PathConfig(
                    mapping_file=str(temp_root / "slot_mappings.json"),
                    log_file=str(temp_root / "app.log"),
                    profile_file=str(profile_path),
                    slot_detail_cache_file=str(temp_root / "slot_detail_cache.json"),
                ),
            )
            service = ProfileBuilderService(str(config_path), str(profile_path))

            profile, updated_existing = service.save_profile(
                EnclosureProfileRequest(
                    source_profile_id=GENERIC_FRONT_24_1X24_PROFILE_ID,
                    id="custom-front-24",
                    label="Custom Front 24",
                    eyebrow="Custom / Front View",
                    summary="Reusable custom front-drive layout.",
                    panel_title="Front 24 Bay",
                    edge_label="Front of chassis",
                    face_style="front-drive",
                    latch_edge="top",
                    bay_size="2.5",
                    rows=1,
                    columns=24,
                    slot_count=24,
                ),
                settings,
            )

            self.assertFalse(updated_existing)
            self.assertEqual(profile.id, "custom-front-24")
            self.assertEqual(profile.slot_layout, [list(range(24))])
            self.assertTrue(profile_path.exists())
            self.assertIn("custom-front-24", profile_path.read_text(encoding="utf-8"))

    def test_save_profile_generates_rectangular_layout_for_new_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config.yaml"
            profile_path = temp_root / "profiles.yaml"
            settings = Settings(
                config_file=str(config_path),
                paths=PathConfig(
                    mapping_file=str(temp_root / "slot_mappings.json"),
                    log_file=str(temp_root / "app.log"),
                    profile_file=str(profile_path),
                    slot_detail_cache_file=str(temp_root / "slot_detail_cache.json"),
                ),
            )
            service = ProfileBuilderService(str(config_path), str(profile_path))

            profile, _ = service.save_profile(
                EnclosureProfileRequest(
                    source_profile_id=GENERIC_FRONT_24_1X24_PROFILE_ID,
                    id="custom-front-6",
                    label="Custom Front 6",
                    summary="Generated rectangular test profile.",
                    face_style="front-drive",
                    latch_edge="right",
                    bay_size="3.5",
                    rows=2,
                    columns=4,
                    slot_count=6,
                    row_groups=[2, 2],
                ),
                settings,
            )

            self.assertEqual(profile.id, "custom-front-6")
            self.assertEqual(profile.row_groups, [2, 2])
            self.assertEqual(profile.slot_layout, [[4, 5], [0, 1, 2, 3]])

    def test_save_profile_respects_explicit_custom_slot_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config.yaml"
            profile_path = temp_root / "profiles.yaml"
            settings = Settings(
                config_file=str(config_path),
                paths=PathConfig(
                    mapping_file=str(temp_root / "slot_mappings.json"),
                    log_file=str(temp_root / "app.log"),
                    profile_file=str(profile_path),
                    slot_detail_cache_file=str(temp_root / "slot_detail_cache.json"),
                ),
            )
            service = ProfileBuilderService(str(config_path), str(profile_path))

            profile, _ = service.save_profile(
                EnclosureProfileRequest(
                    source_profile_id=GENERIC_FRONT_24_1X24_PROFILE_ID,
                    id="custom-front-6-column",
                    label="Custom Front 6 Column",
                    summary="Custom ordering test profile.",
                    face_style="front-drive",
                    latch_edge="right",
                    bay_size="3.5",
                    rows=3,
                    columns=2,
                    slot_count=6,
                    slot_layout=[[2, 5], [1, 4], [0, 3]],
                ),
                settings,
            )

            self.assertEqual(profile.id, "custom-front-6-column")
            self.assertEqual(profile.slot_layout, [[2, 5], [1, 4], [0, 3]])

    def test_request_rejects_slot_layout_when_visible_count_mismatches_slot_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "slot_layout must contain exactly slot_count visible slots"):
            EnclosureProfileRequest(
                source_profile_id=GENERIC_FRONT_24_1X24_PROFILE_ID,
                id="invalid-custom-front-6",
                label="Invalid Custom Front 6",
                summary="Broken slot layout test profile.",
                face_style="front-drive",
                latch_edge="right",
                bay_size="3.5",
                rows=3,
                columns=2,
                slot_count=6,
                slot_layout=[[2, 5], [1, 4], [0]],
            )

    def test_delete_profile_blocks_custom_profiles_still_referenced_by_saved_systems(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config.yaml"
            profile_path = temp_root / "profiles.yaml"
            custom_profile = EnclosureProfileConfig(
                id="custom-front-24",
                label="Custom Front 24",
                rows=1,
                columns=24,
                face_style="front-drive",
                latch_edge="top",
                bay_size="2.5",
                slot_layout=[list(range(24))],
            )
            settings = Settings(
                config_file=str(config_path),
                paths=PathConfig(
                    mapping_file=str(temp_root / "slot_mappings.json"),
                    log_file=str(temp_root / "app.log"),
                    profile_file=str(profile_path),
                    slot_detail_cache_file=str(temp_root / "slot_detail_cache.json"),
                ),
                profiles=[custom_profile],
                systems=[
                    SystemConfig(
                        id="archive-core",
                        label="Archive CORE",
                        default_profile_id="custom-front-24",
                        truenas=TrueNASConfig(platform="core"),
                    )
                ],
                default_system_id="archive-core",
            )
            service = ProfileBuilderService(str(config_path), str(profile_path))
            service._write_profiles([custom_profile])

            with self.assertRaisesRegex(ValueError, "still referenced"):
                service.delete_profile("custom-front-24", settings)
