from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app.config import (
    EnclosureProfileConfig, PathConfig, Settings, StorageViewConfig, SystemConfig,
    TrueNASConfig, load_settings,
)
from app.models.domain import EnclosureProfileRequest
from app.services.profile_builder import ProfileBuilderService
from app.services.profile_registry import ProfileRegistry
from app.services.profile_registry import DELL_MD1280_DRAWER_BOTTOM_PROFILE_ID, GENERIC_FRONT_24_1X24_PROFILE_ID
from app.services.storage_views import storage_view_slot_label


class ProfileBuilderServiceTests(unittest.TestCase):
    def _reload_saved_profile(
        self, service: ProfileBuilderService, profile: EnclosureProfileConfig,
    ) -> EnclosureProfileConfig:
        # Use the production settings loader, not a model copy of the saved return value.
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(service.config_path)}, clear=True):
            settings = load_settings()
        reloaded = next(item for item in settings.profiles if item.id == profile.id)
        self.assertEqual(reloaded.model_dump(), profile.model_dump())
        view = ProfileRegistry(settings).get(profile.id)
        assert view is not None
        assert profile.slot_layout is not None
        self.assertEqual(view.model_dump(mode="json")["slot_number_base"], profile.slot_number_base)
        self.assertEqual(view.slot_layout, profile.slot_layout)
        storage_view = StorageViewConfig(
            id="synthetic-shelf", label="Synthetic shelf", kind="ses_enclosure",
            template_id="ses-auto", profile_id=profile.id,
        )
        for row in profile.slot_layout:
            for slot in row:
                if slot is not None:
                    self.assertEqual(
                        storage_view_slot_label(storage_view, slot, selected_profile=view),
                        f"{slot + (profile.slot_number_base or 0):02d}",
                    )
        return reloaded

    def test_clone_preserves_one_based_drawer_through_save_reload(self) -> None:
        source = ProfileRegistry(Settings()).get(DELL_MD1280_DRAWER_BOTTOM_PROFILE_ID)
        assert source is not None
        source_before = source.model_dump()
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ProfileBuilderService(str(Path(temp_dir) / "config.yaml"), str(Path(temp_dir) / "profiles.yaml"))
            payload = source.model_dump(exclude={"slot_number_base"})
            payload.update(id="custom-drawer", label="Custom drawer", source_profile_id=source.id)
            saved, updated = service.save_profile(EnclosureProfileRequest.model_validate(payload), Settings())
            self.assertFalse(updated)
            self.assertEqual(saved.slot_number_base, 1)
            expected = dict(source_before, id="custom-drawer", label="Custom drawer")
            self.assertEqual(saved.model_dump(), expected)
            self._reload_saved_profile(service, saved)
            self.assertEqual(source.model_dump(), source_before)
            unchanged_source = ProfileRegistry(Settings()).get(source.id)
            assert unchanged_source is not None
            self.assertEqual(unchanged_source.model_dump(), source_before)
            # Drawer 43-84 still addresses internal slots 42-83.
            assert saved.slot_layout is not None
            self.assertEqual(
                sorted(slot for row in saved.slot_layout for slot in row if slot is not None), list(range(42, 84)),
            )

    def test_label_edit_preserves_existing_base_instead_of_clone_source(self) -> None:
        for base in (None, 0, 1, -2, 7):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as temp_dir:
                service = ProfileBuilderService(str(Path(temp_dir) / "config.yaml"), str(Path(temp_dir) / "profiles.yaml"))
                existing = EnclosureProfileConfig(
                    id="custom", label="Original", rows=1, columns=2,
                    slot_layout=[[9, 3]], slot_hints={9: ["synthetic-hint"]}, slot_number_base=base,
                )
                unrelated = existing.model_copy(update={"id": "unrelated", "label": "Unrelated"})
                service._write_profiles([existing, unrelated])
                payload = existing.model_dump(exclude={"slot_number_base"})
                payload.update(label="Renamed", source_profile_id=DELL_MD1280_DRAWER_BOTTOM_PROFILE_ID)
                saved, updated = service.save_profile(EnclosureProfileRequest.model_validate(payload), Settings())
                self.assertTrue(updated)
                self.assertEqual(saved.model_dump(), dict(existing.model_dump(), label="Renamed"))
                self._reload_saved_profile(service, saved)
                self.assertEqual(service._load_profiles()[1].model_dump(), unrelated.model_dump())

    def test_explicit_base_updates_and_null_clears_saved_override(self) -> None:
        # Match the existing config's int-or-null domain, not a new zero/one-only restriction.
        for base in (None, 0, 1, -2, 7, "1"):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as temp_dir:
                service = ProfileBuilderService(str(Path(temp_dir) / "config.yaml"), str(Path(temp_dir) / "profiles.yaml"))
                existing = EnclosureProfileConfig(
                    id="custom", label="Custom", rows=1, columns=2, slot_layout=[[9, 3]], slot_number_base=1,
                )
                service._write_profiles([existing])
                payload = dict(existing.model_dump(), slot_number_base=base)
                expected = EnclosureProfileConfig.model_validate(payload)
                saved, updated = service.save_profile(EnclosureProfileRequest.model_validate(payload), Settings())
                self.assertTrue(updated)
                self.assertEqual(saved.model_dump(), expected.model_dump())
                self._reload_saved_profile(service, saved)
                on_disk = yaml.safe_load(service.profile_path.read_text(encoding="utf-8"))["profiles"][0]
                if base is None:
                    self.assertNotIn("slot_number_base", on_disk)
                else:
                    self.assertEqual(on_disk["slot_number_base"], expected.slot_number_base)

    def test_new_profile_base_controls_and_omission(self) -> None:
        for supplied in ({}, {"slot_number_base": None}, {"slot_number_base": 0}, {"slot_number_base": 1}):
            with self.subTest(supplied=supplied), tempfile.TemporaryDirectory() as temp_dir:
                service = ProfileBuilderService(str(Path(temp_dir) / "config.yaml"), str(Path(temp_dir) / "profiles.yaml"))
                request = EnclosureProfileRequest.model_validate(dict(label="New", rows=1, columns=2, **supplied))
                self.assertEqual("slot_number_base" in request.model_fields_set, "slot_number_base" in supplied)
                saved, updated = service.save_profile(request, Settings())
                self.assertFalse(updated)
                self.assertEqual(saved.slot_number_base, supplied.get("slot_number_base"))
                self._reload_saved_profile(service, saved)

    def test_base_survives_geometry_change_for_clone_and_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ProfileBuilderService(str(Path(temp_dir) / "config.yaml"), str(Path(temp_dir) / "profiles.yaml"))
            saved, _ = service.save_profile(EnclosureProfileRequest(
                id="resized", label="Resized", rows=1, columns=2,
                source_profile_id=DELL_MD1280_DRAWER_BOTTOM_PROFILE_ID,
            ), Settings())
            self.assertEqual(saved.slot_number_base, 1)
            self.assertEqual(saved.slot_layout, [[0, 1]])
            self._reload_saved_profile(service, saved)
            saved, updated = service.save_profile(EnclosureProfileRequest(
                id="resized", label="Resized again", rows=1, columns=3,
            ), Settings())
            self.assertTrue(updated)
            self.assertEqual(saved.slot_number_base, 1)
            self.assertEqual(saved.slot_layout, [[0, 1, 2]])
            self._reload_saved_profile(service, saved)

    def test_invalid_base_is_rejected_before_profile_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ProfileBuilderService(str(Path(temp_dir) / "config.yaml"), str(Path(temp_dir) / "profiles.yaml"))
            original = EnclosureProfileConfig(id="custom", label="Original", rows=1, columns=1, slot_number_base=1)
            service._write_profiles([original])
            before = service.profile_path.read_bytes()
            for base in ("invalid", "", 1.5, [], {}):
                payload = dict(original.model_dump(), slot_number_base=base)
                with self.subTest(base=base):
                    with self.assertRaises(ValueError):
                        EnclosureProfileConfig.model_validate(payload)
                    with self.assertRaises(ValueError):
                        service.save_profile(EnclosureProfileRequest.model_validate(payload), Settings())
                    self.assertEqual(service.profile_path.read_bytes(), before)

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

    def test_profile_models_accept_noncontiguous_slot_ids_above_visible_count(self) -> None:
        for model in (EnclosureProfileConfig, EnclosureProfileRequest):
            with self.subTest(model=model.__name__):
                profile = model(
                    id="noncontiguous-slot-ids",
                    label="Noncontiguous Slot Ids",
                    rows=1,
                    columns=2,
                    slot_count=2,
                    slot_layout=[[9, 3]],
                )

                self.assertEqual(profile.slot_layout, [[9, 3]])
                self.assertEqual(profile.slot_count, 2)

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

            reloaded_profiles = service._load_profiles()

            self.assertEqual(len(reloaded_profiles), 1)
            self.assertEqual(reloaded_profiles[0].slot_layout, [[9, None], [None, 3]])
            self.assertEqual(reloaded_profiles[0].slot_count, 2)

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
