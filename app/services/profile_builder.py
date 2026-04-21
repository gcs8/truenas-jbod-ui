from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml

from app.config import EnclosureProfileConfig, Settings, _load_profile_yaml, normalize_text
from app.models.domain import EnclosureProfileRequest
from app.services.profile_registry import ProfileRegistry, built_in_profile_ids, default_slot_layout


_PROFILE_WRITE_LOCK = threading.Lock()


def _normalize_profile_id(value: str | None, fallback_index: int) -> str:
    text = normalize_text(value)
    if not text:
        return f"custom-profile-{fallback_index}"
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip()).strip("-_").lower()
    return normalized or f"custom-profile-{fallback_index}"


def _layout_slot_count(layout: list[list[int | None]] | None) -> int:
    return sum(
        1
        for row in (layout or [])
        for slot in row
        if isinstance(slot, int)
    )


def _normalize_row_groups(row_groups: list[int], columns: int) -> list[int]:
    normalized = [int(group) for group in row_groups if isinstance(group, int) and int(group) > 0]
    if not normalized:
        return []
    if sum(normalized) != int(columns):
        raise ValueError(f"Row groups must add up to the column count ({columns}).")
    if len(normalized) == 1:
        return []
    return normalized


def collect_profile_references(settings: Settings) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}

    def bucket(profile_id: str) -> dict[str, Any]:
        return references.setdefault(
            profile_id,
            {
                "default_system_ids": [],
                "storage_view_refs": [],
                "enclosure_binding_refs": [],
                "count": 0,
            },
        )

    for system in settings.systems:
        if normalize_text(system.default_profile_id):
            entry = bucket(system.default_profile_id)
            entry["default_system_ids"].append(system.id)
            entry["count"] += 1

        for enclosure_id, profile_id in (system.enclosure_profiles or {}).items():
            normalized_profile_id = normalize_text(profile_id)
            if not normalized_profile_id:
                continue
            entry = bucket(normalized_profile_id)
            entry["enclosure_binding_refs"].append(
                {
                    "system_id": system.id,
                    "system_label": system.label or system.id,
                    "enclosure_id": str(enclosure_id),
                }
            )
            entry["count"] += 1

        for storage_view in system.storage_views or []:
            if not normalize_text(storage_view.profile_id):
                continue
            entry = bucket(storage_view.profile_id)
            entry["storage_view_refs"].append(
                {
                    "system_id": system.id,
                    "system_label": system.label or system.id,
                    "view_id": storage_view.id,
                    "view_label": storage_view.label,
                }
            )
            entry["count"] += 1

    return references


class ProfileBuilderService:
    def __init__(self, config_path: str, profile_path: str) -> None:
        self.config_path = Path(config_path)
        self.profile_path = Path(profile_path)
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)

    def save_profile(
        self,
        payload: EnclosureProfileRequest,
        settings: Settings,
    ) -> tuple[EnclosureProfileConfig, bool]:
        with _PROFILE_WRITE_LOCK:
            profiles = self._load_profiles()
            next_index = len(profiles) + 1
            profile_id = _normalize_profile_id(payload.id or payload.label, next_index)

            existing_index = next(
                (index for index, profile in enumerate(profiles) if profile.id == profile_id),
                None,
            )
            existing_profile = profiles[existing_index] if existing_index is not None else None

            if profile_id in built_in_profile_ids() and existing_profile is None:
                raise ValueError(
                    f"Profile id '{profile_id}' is reserved by a built-in profile. Save this as a new custom id instead."
                )

            slot_count = int(payload.slot_count or 0) or _layout_slot_count(payload.slot_layout)
            if slot_count <= 0:
                slot_count = int(payload.rows) * int(payload.columns)

            registry = ProfileRegistry(settings)
            source_profile = registry.get(payload.source_profile_id) if payload.source_profile_id else None

            slot_layout: list[list[int | None]]
            slot_hints: dict[int, list[str]]
            if payload.slot_layout is not None:
                slot_layout = [list(row) for row in payload.slot_layout]
                slot_hints = {
                    int(slot): list(hints)
                    for slot, hints in (payload.slot_hints or {}).items()
                }
            elif (
                existing_profile is not None
                and int(existing_profile.rows) == int(payload.rows)
                and int(existing_profile.columns) == int(payload.columns)
                and _layout_slot_count(existing_profile.slot_layout) == slot_count
            ):
                slot_layout = [list(row) for row in (existing_profile.slot_layout or [])]
                slot_hints = {
                    int(slot): list(hints)
                    for slot, hints in (existing_profile.slot_hints or {}).items()
                }
            elif (
                source_profile is not None
                and int(source_profile.rows) == int(payload.rows)
                and int(source_profile.columns) == int(payload.columns)
                and _layout_slot_count(source_profile.slot_layout) == slot_count
            ):
                slot_layout = [list(row) for row in source_profile.slot_layout]
                slot_hints = {
                    int(slot): list(hints)
                    for slot, hints in (source_profile.slot_hints or {}).items()
                }
            else:
                if slot_count > int(payload.rows) * int(payload.columns):
                    raise ValueError(
                        f"Visible bay count {slot_count} cannot exceed the rectangular grid capacity of "
                        f"{int(payload.rows) * int(payload.columns)}."
                    )
                slot_layout = default_slot_layout(int(payload.rows), int(payload.columns), slot_count)
                slot_hints = {}

            profile = EnclosureProfileConfig(
                id=profile_id,
                label=payload.label,
                eyebrow=payload.eyebrow,
                summary=payload.summary,
                panel_title=payload.panel_title,
                edge_label=payload.edge_label,
                face_style=payload.face_style,
                latch_edge=payload.latch_edge,
                bay_size=payload.bay_size,
                rows=int(payload.rows),
                columns=int(payload.columns),
                slot_layout=slot_layout,
                row_groups=_normalize_row_groups(payload.row_groups, int(payload.columns)),
                slot_hints=slot_hints,
            )

            if existing_index is None:
                profiles.append(profile)
            else:
                profiles[existing_index] = profile

            self._write_profiles(profiles)
            return profile, existing_index is not None

    def delete_profile(self, profile_id: str, settings: Settings) -> str:
        normalized_profile_id = _normalize_profile_id(profile_id, 1)
        if normalized_profile_id in built_in_profile_ids():
            raise ValueError(f"Built-in profile '{normalized_profile_id}' cannot be deleted.")

        references = collect_profile_references(settings).get(normalized_profile_id, {})
        reference_count = int(references.get("count", 0) or 0)
        if reference_count > 0:
            examples: list[str] = []
            examples.extend(references.get("default_system_ids") or [])
            examples.extend(
                f"{item['system_id']}/{item['view_id']}"
                for item in (references.get("storage_view_refs") or [])
            )
            examples.extend(
                f"{item['system_id']}:{item['enclosure_id']}"
                for item in (references.get("enclosure_binding_refs") or [])
            )
            sample = ", ".join(examples[:3])
            if len(examples) > 3:
                sample = f"{sample}, ..."
            raise ValueError(
                f"Profile '{normalized_profile_id}' is still referenced by {reference_count} saved config item(s): {sample}"
            )

        with _PROFILE_WRITE_LOCK:
            profiles = self._load_profiles()
            existing_index = next(
                (index for index, profile in enumerate(profiles) if profile.id == normalized_profile_id),
                None,
            )
            if existing_index is None:
                raise ValueError(f"Custom profile '{normalized_profile_id}' does not exist in profiles.yaml.")

            removed = profiles.pop(existing_index)
            self._write_profiles(profiles)
            return removed.label or removed.id

    def _load_profiles(self) -> list[EnclosureProfileConfig]:
        if not self.profile_path.exists():
            return []
        loaded = _load_profile_yaml(self.profile_path)
        return [EnclosureProfileConfig.model_validate(profile) for profile in (loaded.get("profiles") or [])]

    def _write_profiles(self, profiles: list[EnclosureProfileConfig]) -> None:
        payload = {
            "profiles": [
                profile.model_dump(mode="python", exclude_none=True)
                for profile in profiles
            ]
        }
        temp_path = self.profile_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(
                payload,
                handle,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=False,
            )
        temp_path.replace(self.profile_path)
