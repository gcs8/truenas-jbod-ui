"""A node-local QuantaStor path is not a cross-node pool or bay identity (#1211).

Synthetic fixtures only. Two nodes report different disks at the same local
path (/dev/sda). Node B's pool-device row must not move node A's disk to
another bay or give it node B's pool, whatever the input order.
"""

from __future__ import annotations

import unittest

from app.config import (
    EnclosureProfileConfig,
    LayoutConfig,
    PathConfig,
    Settings,
    SystemConfig,
    TrueNASConfig,
)
from app.services.inventory import InventoryService
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.truenas_ws import TrueNASRawData


class _NoMappings:
    def load_lookup_snapshot(self):
        return {}

    def get_mapping(self, *args, **kwargs):
        return None

    def has_legacy_only_mapping(self, *args, **kwargs):
        return False


def _service() -> InventoryService:
    profile = EnclosureProfileConfig(
        id="synthetic-two-bay", label="Synthetic two bays", rows=1, columns=2, slot_layout=[[0, 1]],
    )
    system = SystemConfig(
        id="synthetic-qs",
        label="Synthetic QuantaStor",
        truenas=TrueNASConfig(platform="quantastor"),
        default_profile_id=profile.id,
    )
    settings = Settings(
        systems=[system],
        profiles=[profile],
        layout=LayoutConfig(slot_count=2, rows=1, columns=2, api_slot_number_base=0),
        # Never read: nothing in these tests touches a store or a config file.
        config_file="/nonexistent/synthetic.yaml",
        paths=PathConfig(**{name: f"/nonexistent/{name}" for name in PathConfig.model_fields}),
    )
    return InventoryService(settings, system, None, None, None, _NoMappings(), ProfileRegistry(settings))


DISK_A = {
    "id": "disk-a", "storageSystemId": "node-a", "enclosureId": "shelf-a", "devicePath": "/dev/sda",
    "serialNumber": "SANITIZED-QS-A", "slot": 0, "healthStatus": "ONLINE",
}
DISK_B = {
    "id": "disk-b", "storageSystemId": "node-b", "enclosureId": "shelf-b", "devicePath": "/dev/sda",
    "serialNumber": "SANITIZED-QS-B", "slot": 1, "healthStatus": "ONLINE",
}
POOL_B = {"id": "pool-b", "name": "FOREIGN-POOL", "storageSystemId": "node-b"}
MEMBER_B = {
    "id": "member-b", "physicalDiskId": "disk-b", "physicalDiskSerialNumber": "SANITIZED-QS-B",
    "storageSystemId": "node-b", "storagePoolId": "pool-b", "devicePath": "/dev/sda", "slot": 1,
    "raidGroupId": "foreign-vdev", "status": "ONLINE",
}


def _raw(*, disks, pools=(), pool_devices=(), systems=({"id": "node-a"}, {"id": "node-b"})) -> TrueNASRawData:
    return TrueNASRawData(
        enclosures=[], disks=list(disks), pools=list(pools), pool_devices=list(pool_devices),
        systems=list(systems), disk_temperatures={}, smart_test_results=[],
    )


def _bays(service: InventoryService, raw: TrueNASRawData) -> list[tuple]:
    slots, *_ = service._correlate(raw, ParsedSSHData(), [], selected_enclosure_id="node-a")
    return [
        (slot.slot, slot.serial, slot.present, slot.pool_name, slot.vdev_name, slot.topology_label)
        for slot in slots
    ]


class QuantastorPoolDeviceScopeTests(unittest.TestCase):
    HOME = [
        (0, "SANITIZED-QS-A", True, None, None, None),
        (1, None, False, None, None, None),
    ]

    def test_without_the_foreign_row_the_disk_sits_in_its_own_bay(self) -> None:
        # Baseline for the comparison below; passes before and after the fix.
        service = _service()
        raw = _raw(disks=[DISK_A, DISK_B])
        self.assertEqual(service._build_quantastor_disk_records(raw, "node-a")[0].slot, 0)
        self.assertEqual(_bays(service, raw), self.HOME)

    def test_foreign_pool_row_at_the_same_local_path_changes_nothing(self) -> None:
        service = _service()
        for order in (1, -1):
            with self.subTest(order=order):
                raw = _raw(disks=[DISK_A, DISK_B][::order], pools=[POOL_B], pool_devices=[MEMBER_B])
                records = service._build_quantastor_disk_records(raw, "node-a")
                self.assertEqual([(record.serial, record.slot) for record in records], [("SANITIZED-QS-A", 0)])
                self.assertNotIn("quantastor_pool_device", records[0].raw)
                self.assertEqual(_bays(service, raw), self.HOME)

    def test_contradictory_serial_on_an_own_node_row_is_not_admitted(self) -> None:
        # Same owner and same path, but the row names another disk: unknown, not a match.
        service = _service()
        own_row = {**MEMBER_B, "storageSystemId": "node-a", "storagePoolId": "pool-a", "physicalDiskId": None}
        raw = _raw(disks=[DISK_A], pools=[{"id": "pool-a", "name": "HOME-POOL", "storageSystemId": "node-a"}],
                   pool_devices=[own_row])
        records = service._build_quantastor_disk_records(raw, "node-a")
        self.assertEqual(records[0].slot, 0)
        self.assertNotIn("quantastor_pool_device", records[0].raw)
        self.assertEqual(_bays(service, raw), self.HOME)

    def test_own_node_row_at_the_local_path_is_still_admitted(self) -> None:
        # Control: a coherent row from the disk's own node keeps placement and topology.
        service = _service()
        own_row = {
            "id": "member-a", "storageSystemId": "node-a", "storagePoolId": "pool-a",
            "devicePath": "/dev/sda", "slot": 1, "raidGroupId": "home-vdev", "status": "ONLINE",
        }
        pools = [{"id": "pool-a", "name": "HOME-POOL", "storageSystemId": "node-a"}]
        for pool_devices in ([own_row], [own_row, MEMBER_B], [MEMBER_B, own_row]):
            with self.subTest(rows=[row["id"] for row in pool_devices]):
                raw = _raw(disks=[DISK_A, DISK_B], pools=[*pools, POOL_B], pool_devices=pool_devices)
                records = service._build_quantastor_disk_records(raw, "node-a")
                self.assertEqual(records[0].raw.get("quantastor_pool_device"), own_row)
                self.assertEqual(
                    _bays(service, raw),
                    [
                        (0, None, False, None, None, None),
                        (1, "SANITIZED-QS-A", True, "HOME-POOL", "home-vdev",
                         "HOME-POOL > home-vdev > data (active on node-a)"),
                    ],
                )

    # An HA peer reports the same physical disk by serial under its own disk id
    # and its own local path.
    PEER_ROW = {
        "id": "member-shared", "physicalDiskId": "peer-disk-a", "physicalDiskSerialNumber": "SANITIZED-QS-A",
        "storageSystemId": "node-b", "storagePoolId": "pool-b", "devicePath": "/dev/sdq", "slot": 1,
        "raidGroupId": "shared-vdev", "status": "ONLINE",
    }

    def test_coherent_shared_disk_row_from_the_peer_node_is_admitted(self) -> None:
        # Control: stable identity (the serial), not the path, admits the peer's row.
        service = _service()
        for disks in ([DISK_A, DISK_B], [DISK_B, DISK_A]):
            with self.subTest(first=disks[0]["id"]):
                raw = _raw(disks=disks, pools=[POOL_B], pool_devices=[self.PEER_ROW])
                self.assertEqual(
                    _bays(service, raw),
                    [
                        (0, None, False, None, None, None),
                        (1, "SANITIZED-QS-A", True, "FOREIGN-POOL", "shared-vdev",
                         "FOREIGN-POOL > shared-vdev > data (active on node-b)"),
                    ],
                )

    def test_equally_ranked_rows_naming_different_bays_leave_membership_unknown(self) -> None:
        # Ranking orders admitted rows. Two equally ranked rows that name
        # different bays are a conflict to report as unknown, not a choice to
        # make by input order.
        service = _service()
        other_row = {**self.PEER_ROW, "id": "member-other", "slot": 0, "raidGroupId": "other-vdev"}
        for pool_devices in ([self.PEER_ROW, other_row], [other_row, self.PEER_ROW]):
            with self.subTest(first=pool_devices[0]["id"]):
                raw = _raw(disks=[{**DISK_A, "slot": None}], pools=[POOL_B], pool_devices=pool_devices)
                record = service._build_quantastor_disk_records(raw, "node-a")[0]
                self.assertIsNone(record.slot)
                self.assertNotIn("quantastor_pool_device", record.raw)

    def test_a_key_shared_by_many_rows_leaves_membership_unknown_with_bounded_work(self) -> None:
        # Many disks and many rows that all share one serial: each disk must not
        # examine every row (work counted in rows checked, not wall time).
        from unittest import mock

        from app.services import inventory

        service = _service()
        disks = [{**DISK_A, "id": f"disk-{index}", "devicePath": f"/dev/sd{index}"} for index in range(200)]
        rows = [{**self.PEER_ROW, "id": f"member-{index}", "physicalDiskId": f"peer-{index}"} for index in range(200)]
        raw = _raw(disks=disks, pools=[POOL_B], pool_devices=rows)
        checked = mock.Mock(wraps=InventoryService._quantastor_pool_device_contradicts)
        with mock.patch.object(InventoryService, "_quantastor_pool_device_contradicts", checked):
            records = service._build_quantastor_disk_records(raw, "node-a")
        self.assertEqual(len(records), 200)
        self.assertEqual(sum("quantastor_pool_device" in record.raw for record in records), 0)
        self.assertLessEqual(checked.call_count, 2 * len(records) * inventory.QUANTASTOR_POOL_DEVICE_KEY_ROWS_MAX)

        # Control: up to the cap, a shared key still admits its coherent row.
        raw = _raw(disks=[DISK_A], pools=[POOL_B], pool_devices=[self.PEER_ROW])
        self.assertIs(service._build_quantastor_disk_records(raw, "node-a")[0].raw["quantastor_pool_device"],
                      self.PEER_ROW)

    def test_a_peer_disk_at_the_same_local_path_does_not_make_this_disk_visible_there(self) -> None:
        service = _service()
        context = {"cluster_node_ids": ["node-a", "node-b"], "selected_view_label": "node-a"}
        cases = [
            # Distinct disks that share only /dev/sda: visible on node A alone.
            ([DISK_A, DISK_B], ["node-a"]),
            # Control: the peer reports the same serial, so the disk is shared.
            ([DISK_A, {**DISK_B, "serialNumber": "SANITIZED-QS-A"}], ["node-a", "node-b"]),
        ]
        for disks, visible_on in cases:
            with self.subTest(visible_on=visible_on):
                raw = _raw(disks=disks)
                slots, *_ = service._correlate(raw, ParsedSSHData(), [], selected_enclosure_id="node-a")
                service._annotate_quantastor_slot_context(slots, raw, "node-a", context)
                self.assertEqual(slots[0].serial, "SANITIZED-QS-A")
                self.assertEqual(slots[0].operator_context["visible_on_labels"], visible_on)


if __name__ == "__main__":
    unittest.main()
