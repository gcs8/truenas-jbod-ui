from __future__ import annotations

from app.config import Settings, SystemConfig
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.ssh_probe import SSHProbe
from app.services.truenas_ws import TrueNASWebsocketClient


class InventoryRegistry:
    """Create and reuse one inventory service per configured system."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mapping_store = MappingStore(settings.paths.mapping_file)
        self.profile_registry = ProfileRegistry(settings)
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
            service = InventoryService(
                settings=self.settings,
                system=system,
                truenas_client=TrueNASWebsocketClient(system.truenas),
                ssh_probe=SSHProbe(system.ssh),
                mapping_store=self.mapping_store,
                profile_registry=self.profile_registry,
            )
            self._services[system.id] = service
        return service
