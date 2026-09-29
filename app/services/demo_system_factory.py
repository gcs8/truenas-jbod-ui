from __future__ import annotations

from pathlib import Path
import re

from app.config import Settings, _normalize_system_id
from app.models.domain import DemoSystemRequest, EnclosureProfileRequest, SystemSetupRequest
from app.services.profile_builder import ProfileBuilderService, _PROFILE_WRITE_LOCK
from app.services.system_setup import SystemSetupService, _CONFIG_WRITE_LOCK


DEFAULT_DEMO_SYSTEM_ID = "demo-builder-lab"
DEFAULT_DEMO_LABEL = "Demo Builder Lab"
DEFAULT_DEMO_PROFILE_SUFFIX = "chassis"


class DemoSystemFactory:
    def __init__(self, config_path: str, profile_path: str) -> None:
        self.config_path = config_path
        self.profile_path = profile_path
        self.profile_service = ProfileBuilderService(config_path, profile_path)
        self.system_service = SystemSetupService(config_path)

    def create_demo_system(
        self,
        payload: DemoSystemRequest,
        settings: Settings,
    ) -> dict[str, object]:
        # Hold the same locks as ordinary editors across preflight and both saves.
        # Lock order is config then profiles; neither editor acquires the other.
        with _CONFIG_WRITE_LOCK, _PROFILE_WRITE_LOCK:
            system_id = payload.system_id or DEFAULT_DEMO_SYSTEM_ID
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", system_id) or len(system_id) > 248:
                raise ValueError("Demo system id must be at most 248 lowercase letters, digits, hyphens or underscores.")
            config = self.system_service._load_config()
            if not config.get("systems") and (config.get("truenas") or {}).get("host"):
                raise ValueError("Save the legacy system in System Setup before adding a demo, so its configuration is preserved.")
            if any(
                _normalize_system_id(item.get("id"), index + 1) == system_id
                for index, item in enumerate(config.get("systems") or [])
                if isinstance(item, dict)
            ):
                raise ValueError(f"System id '{system_id}' already exists. Choose a new demo id.")
            profile_id = f"{system_id}-{DEFAULT_DEMO_PROFILE_SUFFIX}"
            if any(profile.id == profile_id for profile in self.profile_service._load_profiles()):
                raise ValueError(f"Profile id '{profile_id}' already exists. Choose a new demo id.")
            profile_path = Path(self.profile_path)
            previous = profile_path.read_bytes() if profile_path.exists() else None
            try:
                return self._create_new_demo_system(payload, settings)
            except Exception:
                # A rejected/failed config save must not leave a new demo profile.
                if previous is None:
                    profile_path.unlink(missing_ok=True)
                else:
                    temporary = profile_path.with_suffix(".tmp")
                    temporary.write_bytes(previous)
                    temporary.replace(profile_path)
                raise

    def _create_new_demo_system(self, payload: DemoSystemRequest, settings: Settings) -> dict[str, object]:
        system_id = payload.system_id or DEFAULT_DEMO_SYSTEM_ID
        system_label = payload.label or DEFAULT_DEMO_LABEL
        profile_id = f"{system_id}-{DEFAULT_DEMO_PROFILE_SUFFIX}"

        saved_profile, profile_updated = self.profile_service.save_profile(
            EnclosureProfileRequest(
                source_profile_id="generic-front-12-3x4",
                id=profile_id,
                label=f"{system_label} Chassis",
                eyebrow="Synthetic builder/demo chassis",
                summary=(
                    "Local synthetic 12-bay saved chassis profile for testing the profile builder, "
                    "saved enclosure views, and virtual storage-view flows without touching a real appliance."
                ),
                panel_title="Demo Front 12 Bay",
                edge_label="Front of chassis",
                face_style="front-drive",
                latch_edge="top",
                bay_size="2.5",
                rows=3,
                columns=4,
                slot_count=12,
                row_groups=[2, 2],
                slot_layout=[
                    [2, 5, 8, 11],
                    [1, 4, 7, 10],
                    [0, 3, 6, 9],
                ],
            ),
            settings,
        )

        saved_system, updated_existing = self.system_service.save_system(
            SystemSetupRequest(
                system_id=system_id,
                label=system_label,
                platform="linux",
                truenas_host="https://demo-builder.invalid",
                verify_ssl=False,
                ssh_enabled=False,
                default_profile_id=saved_profile.id,
                replace_existing=False,
                make_default=payload.make_default,
                storage_views=[
                    {
                        "id": "demo-chassis",
                        "label": "Demo Chassis",
                        "kind": "ses_enclosure",
                        "template_id": "ses-auto",
                        "profile_id": saved_profile.id,
                        "enabled": True,
                        "order": 10,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "auto",
                            "enclosure_ids": [],
                            "pool_names": [],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": [],
                        },
                    },
                    {
                        "id": "demo-nvme",
                        "label": "Demo 4x NVMe Carrier",
                        "kind": "nvme_carrier",
                        "template_id": "nvme-carrier-4",
                        "enabled": True,
                        "order": 20,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "hybrid",
                            "enclosure_ids": [],
                            "pool_names": ["fast"],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": [],
                        },
                    },
                    {
                        "id": "demo-boot",
                        "label": "Demo Boot Pair",
                        "kind": "boot_devices",
                        "template_id": "satadom-pair-2",
                        "enabled": True,
                        "order": 30,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": True,
                        },
                        "binding": {
                            "mode": "pool",
                            "enclosure_ids": [],
                            "pool_names": ["boot"],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": [],
                        },
                    },
                    {
                        "id": "demo-manual",
                        "label": "Demo Manual Group",
                        "kind": "manual",
                        "template_id": "manual-4",
                        "enabled": True,
                        "order": 40,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "serial",
                            "enclosure_ids": [],
                            "pool_names": [],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": [],
                        },
                    },
                ],
            )
        )

        return {
            "system": saved_system,
            "profile": saved_profile,
            "updated_existing": updated_existing,
            "updated_profile": profile_updated,
        }
