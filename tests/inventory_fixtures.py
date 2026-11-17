"""Shared inventory test fixtures.

`tests.test_inventory` imports `build_inventory_service` back under the same
name, so modules that import it from there keep working.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, SystemConfig
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore


def build_inventory_service(
    settings: Settings,
    system: SystemConfig,
    truenas_client,
    ssh_probe,
    temp_dir: str,
    bmc_service=None,
) -> InventoryService:
    return InventoryService(
        settings,
        system,
        truenas_client,
        ssh_probe,
        bmc_service,
        MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
    )
