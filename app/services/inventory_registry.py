from __future__ import annotations

from app.config import Settings, SystemConfig
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.quantastor_api import QuantastorRESTClient
from app.services.ssh_probe import SSHProbe
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import TrueNASWebsocketClient


class InventoryRegistry:
    """Create and reuse one inventory service per configured system."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mapping_store = MappingStore(settings.paths.mapping_file)
        self.profile_registry = ProfileRegistry(settings)
        self.slot_detail_store = SlotDetailStore(settings.paths.slot_detail_cache_file)
        self._services: dict[str, InventoryService] = {}

    def get_system(self, system_id: str | None) -> SystemConfig:
        selected_id = system_id or self.settings.default_system_id
        for system in self.settings.systems:
            if system.id == selected_id:
                return system
        return next(system for system in self.settings.systems if system.id == self.settings.default_system_id)

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
                mapping_store=self.mapping_store,
                profile_registry=self.profile_registry,
                slot_detail_store=self.slot_detail_store,
            )
            self._services[system.id] = service
        return service

    async def prewarm_all(self, *, warm_smart: bool = False) -> None:
        for system in self.settings.systems:
            service = self.get_service(system.id)
            await service.prewarm_cache(warm_smart=warm_smart)
