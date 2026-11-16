"""ESXi topology lookup must not borrow another controller's member (#1213).

Two RAID controllers can report the same controller-local slot key (``13:0``).
Physical placement already refuses that ambiguous key. The topology index must
refuse it too, so a disk resolved by its own identity keeps its own pool,
topology label and health, whatever order the controllers were listed in.
All inputs are synthetic.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import EnclosureProfileConfig, LayoutConfig, PathConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import ManualMapping, SlotState
from app.services.inventory import InventoryService
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry

OVERLAP_WARNING = "ESXi controller/enclosure coordinates overlap; ambiguous disks remain unplaced."


class _MemoryMappings:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def load_lookup_snapshot(self):
        return {str(index): row for index, row in enumerate(self.rows)}

    def get_mapping(self, system_id, enclosure_id, slot, **_kwargs):
        return next((row for row in self.rows if (row.system_id, row.enclosure_id, row.slot)
                     == (system_id, enclosure_id, slot)), None)

    def has_legacy_only_mapping(self, *_args, **_kwargs):
        return False


class EsxiTopologyAliasScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def service(self, mapped_serial: str | None) -> InventoryService:
        profile = EnclosureProfileConfig(id="synthetic-two-bay", label="Synthetic two bays", rows=1, columns=2,
                                         slot_layout=[[0, 1]])
        system = SystemConfig(id="synthetic-system", label="Synthetic system",
                              truenas=TrueNASConfig(platform="esxi"), default_profile_id=profile.id)
        settings = Settings(
            systems=[system], profiles=[profile],
            layout=LayoutConfig(slot_count=2, rows=1, columns=2, api_slot_number_base=0),
            config_file=str(self.root / "never-read.yaml"),
            paths=PathConfig(**{name: str(self.root / name) for name in PathConfig.model_fields}),
        )
        mappings = [] if mapped_serial is None else [ManualMapping(
            system_id=system.id, enclosure_id=profile.id, slot=0, serial=mapped_serial)]
        return InventoryService(settings, system, None, None, None, _MemoryMappings(mappings),
                                ProfileRegistry(settings))

    @staticmethod
    def source(*, reverse: bool, controllers=((0, "A", "Onln"), (1, "B", "Offln"))) -> ParsedSSHData:
        drives = [dict(controller_id=f"c{index}", enclosure_id="13", slot=0, slot_key="13:0",
                       serial=f"SANITIZED-{letter}", state=state, interface="SAS")
                  for index, letter, state in controllers]
        virtual_drives = [dict(controller_id=f"c{index}", name=f"POOL-{letter}", raid="RAID1",
                               physical_drives=[dict(controller_id=f"c{index}", slot_key="13:0")])
                          for index, letter, _state in controllers]
        if reverse:
            drives.reverse()
        return ParsedSSHData(esxi_storcli_physical_drives=drives, esxi_storcli_virtual_drives=virtual_drives)

    def correlate(self, mapped_serial, source):
        warnings: list[str] = []
        slots, *_ = self.service(mapped_serial)._correlate(None, source, warnings)
        return next(slot for slot in slots if slot.slot == 0), warnings

    def test_mapped_disk_keeps_its_own_topology_and_state_in_both_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                bay, warnings = self.correlate("SANITIZED-A", self.source(reverse=reverse))
                # The placement guard saw the overlap, so this is the ambiguous case.
                self.assertIn(OVERLAP_WARNING, warnings)
                self.assertEqual(bay.mapping_source, "manual")
                self.assertEqual(bay.serial, "SANITIZED-A")
                self.assertEqual(bay.health, "ONLINE")
                self.assertEqual(bay.pool_name, "POOL-A")
                self.assertEqual(bay.topology_label, "POOL-A > RAID1 member > slot 13:0")
                self.assertEqual(bay.state, SlotState.healthy)

    def test_faulted_disk_keeps_its_own_fault_in_both_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                bay, _warnings = self.correlate("SANITIZED-B", self.source(reverse=reverse))
                self.assertEqual(bay.serial, "SANITIZED-B")
                self.assertEqual(bay.topology_label, "POOL-B > RAID1 member > slot 13:0")
                self.assertEqual(bay.state, SlotState.fault)

    def test_unique_controller_local_alias_still_resolves_topology(self):
        bay, warnings = self.correlate(None, self.source(reverse=False, controllers=((0, "A", "Onln"),)))
        self.assertNotIn(OVERLAP_WARNING, warnings)
        self.assertEqual(bay.serial, "SANITIZED-A")
        self.assertEqual(bay.topology_label, "POOL-A > RAID1 member > slot 13:0")
        self.assertEqual(bay.state, SlotState.healthy)

    def test_topology_index_drops_only_aliases_shared_by_several_disks(self):
        service = self.service(None)
        records = service._build_esxi_disk_records(self.source(reverse=False), enclosure_id="synthetic-two-bay")
        members = service._build_esxi_topology_members(records)
        self.assertNotIn("13:0", members)
        self.assertEqual(members["c0:13:0"].pool_name, "POOL-A")
        self.assertEqual(members["c1:13:0"].pool_name, "POOL-B")
        self.assertEqual(members["sanitized-a"].pool_name, "POOL-A")
        self.assertEqual(members["sanitized-b"].pool_name, "POOL-B")


if __name__ == "__main__":
    unittest.main()
