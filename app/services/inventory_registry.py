from __future__ import annotations

import logging

from app.config import Settings, SystemConfig
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.quantastor_api import QuantastorRESTClient
from app.services.sas_fabric_alias_store import SasFabricAliasStore
from app.services.ssh_probe import SSHProbe
from app.services.slot_detail_store import SlotDetailStore
from app.services.supermicro_bmc import SupermicroBMCService
from app.services.truenas_ws import TrueNASWebsocketClient


logger = logging.getLogger(__name__)


class SystemNotConfiguredError(LookupError):
    """Raised when an explicitly selected system is not configured."""

    def __init__(self, system_id: str) -> None:
        self.system_id = system_id
        super().__init__(f"System '{system_id}' is not configured.")


class InventoryRegistry:
    """Create and reuse one inventory service per configured system."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mapping_store = MappingStore(settings.paths.mapping_file)
        self.sas_fabric_alias_store = SasFabricAliasStore(settings.paths.sas_fabric_alias_file)
        self.profile_registry = ProfileRegistry(settings)
        self.slot_detail_store = SlotDetailStore(settings.paths.slot_detail_cache_file)
        removed = self.slot_detail_store.prune_unknown_systems({system.id for system in settings.systems})
        if removed:
            logger.info("Pruned %d stale slot-detail cache rows for unknown systems.", removed)
        self._services: dict[str, InventoryService] = {}

    def get_system(self, system_id: str | None) -> SystemConfig:
        selected_id = self.settings.default_system_id if system_id is None else system_id
        for system in self.settings.systems:
            if system.id == selected_id:
                return system
        if system_id is not None:
            raise SystemNotConfiguredError(system_id)
        return next(system for system in self.settings.systems if system.id == self.settings.default_system_id)

    def has_system(self, system_id: str) -> bool:
        return any(system.id == system_id for system in self.settings.systems)

    def get_service(self, system_id: str | None) -> InventoryService:
        system = self.get_system(system_id)
        service = self._services.get(system.id)
        if service is None:
            if system.truenas.platform == "quantastor":
                api_client = QuantastorRESTClient(system.truenas)
            else:
                api_client = TrueNASWebsocketClient(system.truenas)
            service = InventoryService(
                settings=self.settings,
                system=system,
                truenas_client=api_client,
                ssh_probe=SSHProbe(system.ssh),
                bmc_service=SupermicroBMCService(system.bmc) if system.bmc.enabled else None,
                mapping_store=self.mapping_store,
                profile_registry=self.profile_registry,
                slot_detail_store=self.slot_detail_store,
                sas_fabric_alias_store=self.sas_fabric_alias_store,
            )
            self._services[system.id] = service
        return service

    async def prewarm_all(self, *, warm_smart: bool = False) -> None:
        for system in self.settings.systems:
            service = self.get_service(system.id)
            await service.prewarm_cache(warm_smart=warm_smart)
