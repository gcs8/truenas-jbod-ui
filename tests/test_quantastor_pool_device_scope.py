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
                self.assertIs(record.raw.get("quantastor_pool_device_conflict"), True)

    def test_serial_known_only_from_the_hardware_row_still_refuses_a_contradicting_row(self) -> None:
        # The disk row carries no serial; the matched hardware row does. An
        # own-node pool-device row at the same local path that names another
        # serial is another disk, whichever identity source knew the serial.
        service = _service()
        pools = [{"id": "pool-a", "name": "HOME-POOL", "storageSystemId": "node-a"}]
        disk = {**DISK_A, "serialNumber": None}
        own_row = {**MEMBER_B, "id": "member-own", "storageSystemId": "node-a", "storagePoolId": "pool-a",
                   "physicalDiskId": None}

        def record(serial_field, member):
            hw_row = {"id": "hw-a", "physicalDiskId": "disk-a", "storageSystemId": "node-a",
                      "enclosureId": "shelf-a", "slot": 0, serial_field: "SANITIZED-QS-A"}
            raw = _raw(disks=[disk], pools=pools, pool_devices=[] if member is None else [member])
            raw.hw_disks = [hw_row]
            return service._build_quantastor_disk_records(raw, "node-a")[0]

        for serial_field in ("serialNumber", "serialNum"):
            with self.subTest(hardware_field=serial_field):
                # Controls: the hardware row alone places the disk in bay 0, and
                # a row naming the hardware serial is admitted.
                self.assertEqual(record(serial_field, None).slot, 0)
                coherent = {**own_row, "physicalDiskSerialNumber": "SANITIZED-QS-A"}
                admitted = record(serial_field, coherent)
                self.assertIs(admitted.raw["quantastor_pool_device"], coherent)
                self.assertEqual(admitted.slot, 1)

                rejected = record(serial_field, own_row)
                self.assertNotIn("quantastor_pool_device", rejected.raw)
                self.assertEqual(rejected.slot, 0)

    def test_disk_and_hardware_rows_that_disagree_on_serial_admit_no_row(self) -> None:
        # The record's own sources disagree on the serial, so no single row can
        # be shown to name this disk: membership stays unknown.
        service = _service()
        pools = [{"id": "pool-a", "name": "HOME-POOL", "storageSystemId": "node-a"}]
        hw_row = {"id": "hw-a", "physicalDiskId": "disk-a", "storageSystemId": "node-a",
                  "enclosureId": "shelf-a", "slot": 0, "serialNumber": "SANITIZED-QS-C"}
        for serial in ("SANITIZED-QS-A", "SANITIZED-QS-C"):
            with self.subTest(row_serial=serial):
                row = {"id": "member-own", "storageSystemId": "node-a", "storagePoolId": "pool-a",
                       "devicePath": "/dev/sda", "slot": 1, "physicalDiskSerialNumber": serial}
                raw = _raw(disks=[DISK_A], pools=pools, pool_devices=[row])
                raw.hw_disks = [hw_row]
                self.assertNotIn("quantastor_pool_device", service._build_quantastor_disk_records(raw, "node-a")[0].raw)

    def test_equally_ranked_rows_with_conflicting_membership_are_unknown_in_either_order(self) -> None:
        # Same bay and pool, but a different vdev and member status: a conflict,
        # not a choice to make by input order. The disk stays in its bay, its
        # membership is unknown, and the page carries a warning.
        service = _service()
        first = self.PEER_ROW
        second = {**first, "id": "member-other", "raidGroupId": "other-vdev", "status": "FAULTED"}
        expected_warning = (
            "QuantaStor reported conflicting pool memberships for 1 disk; "
            "its pool, vdev and member status are shown as unknown."
        )
        # Controls: each row alone is admitted and drawn.
        for rows, vdev, state in (([first], "shared-vdev", "healthy"), ([second], "other-vdev", "fault")):
            with self.subTest(single=rows[0]["id"]):
                warnings: list[str] = []
                raw = _raw(disks=[DISK_A], pools=[POOL_B], pool_devices=rows)
                slots, *_ = service._correlate(raw, ParsedSSHData(), warnings, selected_enclosure_id="node-a")
                bay = next(slot for slot in slots if slot.present)
                self.assertEqual((bay.slot, bay.vdev_name, bay.state.value), (1, vdev, state))
                self.assertNotIn(expected_warning, warnings)
        for rows in ([first, second], [second, first]):
            with self.subTest(first=rows[0]["id"]):
                raw = _raw(disks=[DISK_A], pools=[POOL_B], pool_devices=rows)
                record = service._build_quantastor_disk_records(raw, "node-a")[0]
                self.assertNotIn("quantastor_pool_device", record.raw)
                self.assertEqual(record.slot, 0)
                warnings = []
                slots, *_ = service._correlate(raw, ParsedSSHData(), warnings, selected_enclosure_id="node-a")
                bay = next(slot for slot in slots if slot.present)
                self.assertEqual((bay.slot, bay.pool_name, bay.vdev_name), (0, None, None))
                self.assertEqual(warnings.count(expected_warning), 1)

    def test_equally_ranked_rows_with_the_same_membership_are_one_answer(self) -> None:
        # Control: two row IDs that agree on bay, pool, vdev, class and status
        # are the same membership and are admitted in either order, without a warning.
        service = _service()
        twin = {**self.PEER_ROW, "id": "member-twin"}
        for rows in ([self.PEER_ROW, twin], [twin, self.PEER_ROW]):
            with self.subTest(first=rows[0]["id"]):
                warnings: list[str] = []
                raw = _raw(disks=[DISK_A], pools=[POOL_B], pool_devices=rows)
                self.assertIn(service._build_quantastor_disk_records(raw, "node-a")[0].raw.get(
                    "quantastor_pool_device"), rows)
                slots, *_ = service._correlate(raw, ParsedSSHData(), warnings, selected_enclosure_id="node-a")
                bay = next(slot for slot in slots if slot.present)
                self.assertEqual((bay.slot, bay.vdev_name, bay.state.value), (1, "shared-vdev", "healthy"))
                self.assertFalse([warning for warning in warnings if "conflicting pool" in warning])

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
        # Overloaded keys are reported as conflicts, so the page warns about them.
        self.assertEqual(sum(bool(record.raw.get("quantastor_pool_device_conflict")) for record in records), 200)
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
