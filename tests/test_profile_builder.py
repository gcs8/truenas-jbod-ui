from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import EnclosureProfileConfig, PathConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import EnclosureProfileRequest
from app.services.profile_builder import ProfileBuilderService
from app.services.profile_registry import ProfileRegistry
from app.services.profile_registry import GENERIC_FRONT_24_1X24_PROFILE_ID


class ProfileBuilderServiceTests(unittest.TestCase):
    def test_enclosure_profile_config_accepts_sparse_layout_holes(self) -> None:
        profile = EnclosureProfileConfig(
            id="sparse",
            label="Sparse",
            rows=2,
            columns=3,
            slot_layout=[[5, None, 2], [None, 0, None]],
        )

        self.assertEqual(profile.slot_layout, [[5, None, 2], [None, 0, None]])
        self.assertEqual(profile.slot_count, 3)

    def test_enclosure_profile_config_rejects_invalid_layout_geometry_and_ids(self) -> None:
        invalid_layouts = [
            (1, 2, [[0], [1]], "row count"),
            (1, 2, [[0]], "exactly columns"),
            (1, 1, [[0, 1]], "exactly columns"),
            (1, 2, [[0, 0]], "unique"),
            (1, 1, [[-1]], "non-negative"),
            (1, 1, [[1.5]], "integers"),
            (1, 1, [["0"]], "integers"),
            (1, 1, [[""]], "integers"),
        ]
        for rows, columns, slot_layout, message in invalid_layouts:
            for model in (EnclosureProfileConfig, EnclosureProfileRequest):
                with self.subTest(model=model.__name__, slot_layout=slot_layout), self.assertRaisesRegex(
                    ValueError,
                    message,
                ):
                    model(
                        id="invalid",
                        label="Invalid",
                        rows=rows,
                        columns=columns,
                        slot_layout=slot_layout,
                    )

    def test_profile_models_reject_explicit_slot_count_outside_bounds(self) -> None:
        for model in (EnclosureProfileConfig, EnclosureProfileRequest):
            for slot_count in (0, 4097):
                with self.subTest(model=model.__name__, slot_count=slot_count), self.assertRaises(ValueError):
                    model(
                        id="invalid-count",
                        label="Invalid Count",
                        rows=1,
                        columns=1,
                        slot_count=slot_count,
                        slot_layout=[[0]],
                    )

    def test_request_rejects_visible_slot_id_outside_explicit_slot_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "less than slot_count"):
            EnclosureProfileRequest(
                id="invalid-slot-id",
                label="Invalid Slot Id",
                rows=1,
                columns=2,
                slot_count=2,
                slot_layout=[[0, 2]],
            )

        with self.assertRaisesRegex(ValueError, "less than slot_count"):
            EnclosureProfileConfig(
                id="invalid-slot-id",
                label="Invalid Slot Id",
                rows=1,
                columns=2,
                slot_count=2,
                slot_layout=[[0, 2]],
            )

    def test_save_profile_infers_omitted_slot_count_from_visible_sparse_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            service = ProfileBuilderService(
                str(temp_root / "config.yaml"),
                str(temp_root / "profiles.yaml"),
            )
            request = EnclosureProfileRequest(
                id="sparse-non-contiguous",
                label="Sparse Non-contiguous",
                rows=2,
                columns=2,
                slot_layout=[[9, None], [None, 3]],
            )

            self.assertEqual(request.slot_count, 2)

            profile, _ = service.save_profile(request, Settings())

            self.assertEqual(profile.slot_layout, [[9, None], [None, 3]])
            self.assertEqual(profile.slot_count, 2)

    def test_built_in_profile_layouts_keep_their_validated_physical_counts(self) -> None:
        profiles = ProfileRegistry(Settings()).list_profiles()

        self.assertTrue(profiles)
        for profile in profiles:
            with self.subTest(profile_id=profile.id):
                self.assertEqual(len(profile.slot_layout), profile.rows)
                self.assertTrue(all(len(row) == profile.columns for row in profile.slot_layout))
                visible_slots = [slot for row in profile.slot_layout for slot in row if slot is not None]
                self.assertEqual(profile.slot_count, len(visible_slots))
                self.assertEqual(len(visible_slots), len(set(visible_slots)))
                self.assertTrue(all(slot >= 0 for slot in visible_slots))

    def test_unknown_explicit_profile_is_not_reused_as_runtime_profile_id(self) -> None:
        system = SystemConfig(
            id="system-a",
            default_profile_id="misspelled-private-profile",
            truenas=TrueNASConfig(platform="core"),
        )

        profile = ProfileRegistry(Settings()).resolve_for_enclosure(
            system,
            None,
            fallback_rows=1,
            fallback_columns=1,
            fallback_slot_count=1,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, "runtime-enclosure")

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
            self.assertEqual(profile.slot_layout, [[4, 5, None, None], [0, 1, 2, 3]])

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
                slot_layout=[[2, 5], [1, 4], [0, None]],
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
