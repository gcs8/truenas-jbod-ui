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
        super().__init__(f'No system named "{system_id}" is configured.')


class InventoryRegistry:
    """Create and reuse one inventory service per configured system."""

    def __init__(self, settings: Settings, *, previous: InventoryRegistry | None = None) -> None:
        """Build the registry for ``settings``.

        ``previous`` is the registry of the settings this replaces after a
        config reload (#432). Its data stores are reused when their files did
        not move, and each system's new service takes over the previous
        service's appliance answers when the connection settings are the same,
        so a rename does not re-query every appliance.
        """
        self.settings = settings
        reuse_stores = previous is not None and previous.settings.paths == settings.paths
        if reuse_stores:
            assert previous is not None
            self.mapping_store = previous.mapping_store
            self.sas_fabric_alias_store = previous.sas_fabric_alias_store
            self.slot_detail_store = previous.slot_detail_store
        else:
            self.mapping_store = MappingStore(settings.paths.mapping_file)
            self.sas_fabric_alias_store = SasFabricAliasStore(settings.paths.sas_fabric_alias_file)
            self.slot_detail_store = SlotDetailStore(settings.paths.slot_detail_cache_file)
        self.profile_registry = ProfileRegistry(settings)
        system_ids = {system.id for system in settings.systems}
        if previous is None or {system.id for system in previous.settings.systems} != system_ids:
            removed = self.slot_detail_store.prune_unknown_systems(system_ids)
            if removed:
                logger.info("Pruned %d stale slot-detail cache rows for unknown systems.", removed)
        self._services: dict[str, InventoryService] = {}
        # Services of the replaced settings not yet taken over, one level deep
        # so a chain of reloads never keeps old registries alive.
        self._predecessors: dict[str, InventoryService] = {}
        if previous is not None:
            self._predecessors = {
                system_id: service
                for system_id, service in {**previous._predecessors, **previous._services}.items()
                if system_id in system_ids
            }

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
            predecessor = self._predecessors.pop(system.id, None)
            if predecessor is not None:
                service.adopt_caches_from(
                    predecessor,
                    keep_snapshots=self._snapshots_still_valid(predecessor, system),
                )
            self._services[system.id] = service
        return service

    def _snapshots_still_valid(self, predecessor: InventoryService, system: SystemConfig) -> bool:
        """True when nothing a cached snapshot of ``system`` was built from changed.

        The list of system names in each snapshot is refreshed when it is
        carried over, so renaming one system keeps the others' snapshots.
        """
        previous_settings = predecessor.settings
        return (
            predecessor.system == system
            and previous_settings.layout == self.settings.layout
            and previous_settings.profiles == self.settings.profiles
            and [item.id for item in previous_settings.systems] == [item.id for item in self.settings.systems]
        )

    async def prewarm_all(self, *, warm_smart: bool = False) -> None:
        for system in self.settings.systems:
            service = self.get_service(system.id)
            await service.prewarm_cache(warm_smart=warm_smart)
