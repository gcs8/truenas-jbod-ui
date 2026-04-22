from __future__ import annotations

from app.config import Settings
from app.models.domain import DemoSystemRequest, EnclosureProfileRequest, SystemSetupRequest
from app.services.profile_builder import ProfileBuilderService
from app.services.system_setup import SystemSetupService


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
                replace_existing=payload.replace_existing,
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
