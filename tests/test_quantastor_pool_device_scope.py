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


CONFLICT_WARNING_ONE = (
    "QuantaStor gave conflicting pool-device or identity data for 1 disk, so its pool-device "
    "membership and member status are withheld; any pool and health reported for the disk "
    "itself are still shown."
)
CONFLICT_WARNING_MANY = (
    "QuantaStor gave conflicting pool-device or identity data for {count} disks, so their "
    "pool-device membership and member status are withheld; any pool and health reported for "
    "each disk itself are still shown."
)


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


def _counted_rows(rows, reads):
    """Copies of `rows` that count every field read into reads[0]."""

    class CountedRow(dict):
        def get(self, *args):
            reads[0] += 1
            return dict.get(self, *args)

        def __getitem__(self, key):
            reads[0] += 1
            return dict.__getitem__(self, key)

    return [CountedRow(row) for row in rows]


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
        expected_warning = CONFLICT_WARNING_ONE
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

    def test_a_hardware_group_shared_by_many_rows_is_not_read_row_by_row(self) -> None:
        # Every hardware row a disk reaches is an identity source, but rows
        # sharing one key must not be read in full for every disk: past the cap
        # the identity is unknown, and so is the membership. Work is counted in
        # hardware-row field reads, so it does not depend on helper names.
        from app.services import inventory

        cap = inventory.QUANTASTOR_DISK_IDENTITY_ROWS_MAX
        service = _service()

        def shared(count):
            return [{"id": f"hw-{n}", "serialNumber": "SANITIZED-QS-A", "storageSystemId": "node-a",
                     "enclosureId": "shelf-a", "slot": 0} for n in range(count)]

        def run(count):
            reads = [0]
            disks = [{**DISK_A, "id": f"disk-{n}", "devicePath": f"/dev/synthetic-{n}"} for n in range(count)]
            raw = _raw(disks=disks, pools=[POOL_B], pool_devices=[self.PEER_ROW])
            raw.hw_disks = _counted_rows(shared(count), reads)
            return service._build_quantastor_disk_records(raw, "node-a"), reads[0]

        records, reads = run(200)
        self.assertEqual(len(records), 200)
        self.assertEqual(sum("quantastor_pool_device" in record.raw for record in records), 0)
        self.assertEqual(sum(bool(record.raw.get("quantastor_pool_device_conflict")) for record in records), 200)
        # Twice the disks and rows cost about twice the reads; reading the
        # shared rows for every disk would cost about four times as many.
        _records, reads_doubled = run(400)
        self.assertLess(reads_doubled, 3 * reads)

        # Boundary controls: at the cap the rows are read and the coherent row
        # is admitted; one row past it the membership is unknown. The cap
        # counts rows reached, also when no single key is shared by many: a
        # chain of rows each linked to the next by another key.
        def chain(count):
            return [{"id": f"link-{n}", "sasAddress": f"link-{n + 1}",
                     "storageSystemId": "node-a", "enclosureId": "shelf-a", "slot": 0} for n in range(count)]

        for shape, build, disk in (("shared", shared, DISK_A), ("chain", chain, {**DISK_A, "hwDiskId": "link-0"})):
            for size, admitted in ((cap, True), (cap + 1, False)):
                with self.subTest(shape=shape, rows=size):
                    raw = _raw(disks=[disk], pools=[POOL_B], pool_devices=[self.PEER_ROW])
                    raw.hw_disks = build(size)
                    record = service._build_quantastor_disk_records(raw, "node-a")[0]
                    self.assertEqual(record.raw.get("quantastor_pool_device") is self.PEER_ROW, admitted)
                    self.assertEqual(bool(record.raw.get("quantastor_pool_device_conflict")), not admitted)

    def test_hardware_identity_is_indexed_once_not_scanned_for_every_disk(self) -> None:
        # Each disk has its own hardware row among as many unrelated rows. The
        # rows are indexed once per pass, so reads grow with the input, not
        # with disks times rows.
        service = _service()

        def run(count):
            reads = [0]
            disks = [{**DISK_A, "id": f"disk-{n}", "devicePath": f"/dev/synthetic-{n}",
                      "serialNumber": f"SANITIZED-QS-{n}"} for n in range(count)]
            own = [{"id": f"hw-{n}", "physicalDiskId": f"disk-{n}", "serialNumber": f"SANITIZED-QS-{n}",
                    "storageSystemId": "node-a", "enclosureId": "shelf-a", "slot": 0} for n in range(count)]
            unrelated = [{"id": f"hw-other-{n}", "physicalDiskId": f"other-{n}", "serialNumber": f"SANITIZED-QS-X{n}",
                          "storageSystemId": "node-a", "enclosureId": "shelf-a"} for n in range(count)]
            rows = [{"id": f"member-{n}", "storageSystemId": "node-a", "storagePoolId": "pool-a",
                     "devicePath": f"/dev/synthetic-{n}", "slot": 1, "raidGroupId": "vdev-a", "status": "ONLINE"}
                    for n in range(count)]
            raw = _raw(disks=disks, pools=[HOME_POOL], pool_devices=rows)
            raw.hw_disks = _counted_rows(own, reads)
            raw.cli_hw_disks = _counted_rows(unrelated, reads)
            records = service._build_quantastor_disk_records(raw, "node-a")
            return records, rows, reads[0]

        records, rows, reads = run(200)
        self.assertEqual([record.raw.get("quantastor_pool_device") for record in records], rows)
        _records, _rows, reads_doubled = run(400)
        self.assertLess(reads_doubled, 3 * reads)

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


# Generated admission cases. The expectations below are written from the rule,
# not from the implementation: every identity the service holds for a disk is
# gathered first (REST disk row, every hardware row its keys reach, the matched
# CLI row), and the decision is made once over that whole set.
HOME_POOL = {"id": "pool-a", "name": "HOME-POOL", "storageSystemId": "node-a"}
OTHER_POOL = {"id": "pool-c", "name": "OTHER-POOL", "storageSystemId": "node-a"}
# A row of another node at the same local path; it must never be admitted.
DECOY_ROW = {**MEMBER_B, "physicalDiskSerialNumber": "SANITIZED-QS-Z"}
# Identity kinds: (agreeing value, conflicting value, disk-side sources, row-side fields).
# A source is (container, field); "hw1" is reached by the disk's hwDiskId, "hw2"
# by the disk's own ID, "cli" is the CLI row the CLI resolver admits.
IDENTITY_KINDS = {
    "serial": ("SANITIZED-QS-A", "SANITIZED-QS-C",
               (("rest", "serialNumber"), ("rest", "serial"), ("hw1", "serialNumber"), ("hw1", "serialNum"),
                ("hw2", "serialNumber"), ("cli", "serialNumber")),
               (("row", "physicalDiskSerialNumber"), ("nested", "serialNumber"))),
    "scsi": ("scsi-synthetic-a", "scsi-synthetic-c",
             (("rest", "scsiId"), ("hw1", "scsiId"), ("cli", "scsiId")),
             (("row", "physicalDiskScsiId"), ("nested", "scsiId"))),
    "physical": ("disk-a", "disk-c",
                 (("rest", "id"), ("hw1", "physicalDiskId")),
                 (("row", "physicalDiskId"), ("nested", "id"))),
}


# Outcome totals of the generated identity cases, counted from the rule above
# (both orders): serial 3**6 x 6 row states, SCSI 3**3 x 6, physical 3**2 x 6 x
# 2 hardware-row nodes, each in two orders.
IDENTITY_CASE_COUNTS = {"cases": 9288, "admitted": 1284, "refused": 2364, "conflict": 5640}


def _identity_fixture(kind, states, row_state, hw1_node="node-a"):
    """Build one case. `states` maps each source to None, "agree" or "conflict"."""
    agree, conflict, sources, _row_fields = IDENTITY_KINDS[kind]
    value = {None: None, "agree": agree, "conflict": conflict}
    disk = {"storageSystemId": "node-a", "enclosureId": "shelf-a", "devicePath": "/dev/sda", "slot": 0,
            "healthStatus": "ONLINE", "hwDiskId": "hw-a"}
    if kind != "physical":
        disk["id"] = "disk-a"
    hw1 = {"id": "hw-a", "storageSystemId": hw1_node, "enclosureId": "shelf-a", "slot": 0}
    hw2 = {"id": "hw-b", "physicalDiskId": "disk-a", "storageSystemId": "node-a", "enclosureId": "shelf-a",
           "slot": 0}
    cli = {"physicalDiskId": "disk-a", "storageSystemId": "node-a", "devicePath": "/dev/sda"}
    containers = {"rest": disk, "hw1": hw1, "hw2": hw2, "cli": cli}
    for (container, field), state in zip(sources, states):
        if state is not None:
            containers[container][field] = value[state]
    row = {"id": "member-a", "storageSystemId": "node-a", "storagePoolId": "pool-a", "devicePath": "/dev/sda",
           "slot": 1, "raidGroupId": "vdev-a", "status": "ONLINE"}
    if row_state is not None and row_state != "no-row":
        (container, field), state = row_state
        if container == "row":
            row[field] = value[state]
        else:
            row["physicalDiskObj"] = {field: value[state]}
    hw_rows = [hw1] if kind == "physical" else [hw1, hw2]
    cli_rows = [] if kind == "physical" else [cli]
    pool_rows = [] if row_state == "no-row" else [row]
    return disk, hw_rows, cli_rows, pool_rows


def _identity_expectation(kind, states, row_state, hw1_node="node-a"):
    """What the rule says: (admitted, conflict, cli_matched)."""
    agree, conflict, sources, _row_fields = IDENTITY_KINDS[kind]
    value = {None: None, "agree": agree, "conflict": conflict}
    by_source = {source: value[state] for source, state in zip(sources, states)}
    rest = {v for (container, _f), v in by_source.items() if container == "rest" and v}
    # The CLI row is matched only when it does not contradict the REST disk,
    # and never when the REST disk's own serial aliases disagree.
    cli_value = by_source.get(("cli", "serialNumber" if kind == "serial" else "scsiId"))
    cli_matched = kind != "physical" and len(rest) <= 1 and not (cli_value and rest and cli_value not in rest)
    known = set(rest)
    for (container, _field), v in by_source.items():
        if not v or container == "rest":
            continue
        if container == "cli" and not cli_matched:
            continue
        # A hardware row's physical disk ID names this disk only on its own node.
        if kind == "physical" and container == "hw1" and hw1_node != "node-a":
            continue
        known.add(v)
    if row_state == "no-row":
        return False, False, cli_matched
    if len(known) > 1:
        return False, True, cli_matched
    row_value = value[row_state[1]] if row_state is not None else None
    if row_value and known and row_value not in known:
        return False, False, cli_matched
    return True, False, cli_matched


class QuantastorPoolDeviceGeneratedAdmissionTests(unittest.TestCase):
    def _report(self, mismatches: list, total: int) -> None:
        # One summary, so a failure shows how many generated cases differ.
        if mismatches:
            shown = "\n".join(repr(item) for item in mismatches[:12])
            self.fail(f"{len(mismatches)} of {total} generated cases differ from the rule; first ones:\n{shown}")

    def test_every_identity_field_of_every_source_present_absent_or_conflicting(self) -> None:
        import itertools

        service = _service()
        pools = [HOME_POOL, POOL_B]
        counts = {"cases": 0, "admitted": 0, "refused": 0, "conflict": 0}
        mismatches = []
        for kind, (_agree, _conflict, sources, row_fields) in IDENTITY_KINDS.items():
            row_states = ["no-row", None, *((field, state) for field in row_fields for state in ("agree", "conflict"))]
            hw1_nodes = ("node-a", "node-b") if kind == "physical" else ("node-a",)
            for states in itertools.product((None, "agree", "conflict"), repeat=len(sources)):
                for row_state, hw1_node in itertools.product(row_states, hw1_nodes):
                    admitted, conflict, cli_matched = _identity_expectation(kind, states, row_state, hw1_node)
                    disk, hw_rows, cli_rows, rows = _identity_fixture(kind, states, row_state, hw1_node)
                    expected = (cli_matched and bool(cli_rows), admitted, conflict, 1 if admitted else 0)
                    for order in (1, -1):
                        raw = _raw(disks=[disk, DISK_B][::order], pools=pools,
                                   pool_devices=[*rows, DECOY_ROW][::order])
                        raw.hw_disks = hw_rows[::order]
                        raw.cli_disks = cli_rows
                        record = service._build_quantastor_disk_records(raw, "node-a")[0]
                        admitted_row = record.raw.get("quantastor_pool_device")
                        # The first field is a self-check of the fixture: the CLI
                        # row is matched exactly when the CLI resolver's rule says.
                        got = ("quantastor_cli_disk" in record.raw,
                               admitted_row is not None and admitted_row is rows[0],
                               bool(record.raw.get("quantastor_pool_device_conflict")), record.slot)
                        if got != expected:
                            mismatches.append((kind, states, row_state, hw1_node, order, "got", got,
                                               "expected", expected))
                        counts["cases"] += 1
                        counts["admitted" if admitted else "conflict" if conflict else "refused"] += 1
        self._report(mismatches, counts["cases"])
        # Every outcome is reached, so no branch of the rule is vacuous.
        self.assertEqual(counts, IDENTITY_CASE_COUNTS)

    # How a row can be found for a disk: the disk-side fields that make the key
    # known, and the row fields that name it. Each row names only its own key.
    CHANNELS = {
        "own-node-path": ({}, {}, {"devicePath": "/dev/sda"}),
        "rest-serialNumber": ({"serialNumber": "SANITIZED-QS-A"}, {}, {"physicalDiskSerialNumber": "SANITIZED-QS-A"}),
        "rest-serial": ({"serial": "SANITIZED-QS-A"}, {}, {"physicalDiskSerialNumber": "SANITIZED-QS-A"}),
        "hw-serialNumber": ({}, {"serialNumber": "SANITIZED-QS-A"}, {"physicalDiskSerialNumber": "SANITIZED-QS-A"}),
        "hw-serialNum": ({}, {"serialNum": "SANITIZED-QS-A"}, {"physicalDiskObj": {"serialNumber": "SANITIZED-QS-A"}}),
        "rest-scsiId": ({"scsiId": "scsi-synthetic-a"}, {}, {"physicalDiskScsiId": "scsi-synthetic-a"}),
        "hw-scsiId": ({}, {"scsiId": "scsi-synthetic-a"}, {"physicalDiskScsiId": "scsi-synthetic-a"}),
        "rest-id": ({"id": "disk-a"}, {}, {"physicalDiskId": "disk-a"}),
        "hw-physicalDiskId": ({}, {"physicalDiskId": "disk-a"}, {"physicalDiskObj": {"id": "disk-a"}}),
    }
    # Each membership field separately, and a control where nothing differs.
    MEMBERSHIP_CHANGES = {
        "same": {}, "bay": {"slot": 0}, "pool": {"storagePoolId": "pool-c"},
        "vdev": {"raidGroupId": "vdev-c"}, "class": {"class": "log"}, "status": {"status": "FAULTED"},
    }

    def test_equal_ranked_rows_found_through_any_two_identity_sources(self) -> None:
        import itertools

        service = _service()
        counts = {"cases": 0, "conflict": 0, "one-answer": 0}
        mismatches = []
        channel_pairs = list(itertools.combinations_with_replacement(self.CHANNELS, 2))
        for (first_channel, second_channel), (change, fields), disk_pool in itertools.product(
            channel_pairs, self.MEMBERSHIP_CHANGES.items(), (None, "pool-a"),
        ):
            disk = {"storageSystemId": "node-a", "enclosureId": "shelf-a", "devicePath": "/dev/sda", "slot": 0,
                    "healthStatus": "ONLINE", "hwDiskId": "hw-a"}
            if disk_pool:
                disk["storagePoolId"] = disk_pool
            hw = {"id": "hw-a", "storageSystemId": "node-a", "enclosureId": "shelf-a", "slot": 0}
            rows = []
            for number, channel in enumerate((first_channel, second_channel), start=1):
                disk_fields, hw_fields, row_fields = self.CHANNELS[channel]
                disk.update(disk_fields)
                hw.update(hw_fields)
                rows.append({"id": f"member-{number}", "storageSystemId": "node-a", "storagePoolId": "pool-a",
                             "slot": 1, "raidGroupId": "vdev-a", "status": "ONLINE", **row_fields})
            rows[1].update(fields)
            if change == "same":
                # One answer: the same row in either order, drawn, no warning.
                expected = (True, False, (1, "HOME-POOL", "vdev-a", "healthy"), 0)
            else:
                # Unknown membership with one warning; the pool the disk reports
                # for itself is kept, the pool-device membership is not.
                expected = (False, True, (0, "HOME-POOL" if disk_pool else None, None, "healthy"), 1)
            chosen = set()
            for order in (1, -1):
                raw = _raw(disks=[disk], pools=[HOME_POOL, OTHER_POOL], pool_devices=rows[::order])
                raw.hw_disks = [hw]
                record = service._build_quantastor_disk_records(raw, "node-a")[0]
                warnings: list[str] = []
                slots, *_ = service._correlate(raw, ParsedSSHData(), warnings, selected_enclosure_id="node-a")
                bay = next(slot for slot in slots if slot.present)
                admitted_row = record.raw.get("quantastor_pool_device")
                if admitted_row is not None:
                    chosen.add(admitted_row.get("id"))
                got = (admitted_row is not None and any(admitted_row is row for row in rows),
                       record.raw.get("quantastor_pool_device_conflict") is True,
                       (bay.slot, bay.pool_name, bay.vdev_name, bay.state.value),
                       warnings.count(CONFLICT_WARNING_ONE))
                if got != expected:
                    mismatches.append((first_channel, second_channel, change, disk_pool, order, "got", got,
                                       "expected", expected))
                counts["cases"] += 1
                counts["one-answer" if change == "same" else "conflict"] += 1
            if change == "same" and len(chosen) != 1:
                mismatches.append((first_channel, second_channel, change, disk_pool, "order-dependent", sorted(chosen)))
        self._report(mismatches, counts["cases"])
        self.assertEqual(counts, {"cases": 1080, "conflict": 900, "one-answer": 180})

    def test_conflict_warning_counts_disks(self) -> None:
        service = _service()
        disks = [{**DISK_A, "id": f"disk-{n}", "devicePath": f"/dev/synthetic-{n}", "slot": n,
                  "serialNumber": None} for n in range(2)]
        rows = [{"id": f"member-{n}-{v}", "physicalDiskId": f"disk-{n}", "storageSystemId": "node-a",
                 "storagePoolId": "pool-a", "slot": n, "raidGroupId": f"vdev-{v}", "status": "ONLINE"}
                for n in range(2) for v in ("x", "y")]
        warnings: list[str] = []
        service._correlate(_raw(disks=disks, pools=[HOME_POOL], pool_devices=rows), ParsedSSHData(), warnings,
                           selected_enclosure_id="node-a")
        self.assertEqual([w for w in warnings if w.startswith("QuantaStor gave conflicting")],
                         [CONFLICT_WARNING_MANY.format(count=2)])


# One identity per disk, from every hardware row (#1211). A disk's identity is
# every serial and ID on its REST row, then every hardware row any of those keys
# reaches (API and CLI arrays, with or without a slot), then those rows' keys,
# until nothing new is reached. Each case below has a row that is coherent on
# its own, so the control shows the refusal comes from the disagreement.
SERIAL_A = "SANITIZED-QS-A"
SERIAL_C = "SANITIZED-QS-C"
IDENTITY_DISK = {"id": "disk-a", "storageSystemId": "node-a", "enclosureId": "shelf-a", "devicePath": "/dev/sda",
                 "slot": 0, "healthStatus": "ONLINE"}
# Reached by the own-node path only: it names no identity of its own.
PATH_ROW = {"id": "member-a", "storageSystemId": "node-a", "storagePoolId": "pool-a", "devicePath": "/dev/sda",
            "slot": 1, "raidGroupId": "vdev-a", "status": "ONLINE"}


def _hw(row_id, **fields):
    return {"id": row_id, "storageSystemId": "node-a", "enclosureId": "shelf-a", "slot": 0, **fields}


def _membership(disk, *, hw=(), cli_hw=(), pool_devices=(PATH_ROW,)):
    """(admitted row id, conflict flag, conflict warnings) through the shipping builder and page path."""
    service = _service()
    raw = _raw(disks=[disk], pools=[HOME_POOL], pool_devices=list(pool_devices))
    raw.hw_disks = list(hw)
    raw.cli_hw_disks = list(cli_hw)
    record = service._build_quantastor_disk_records(raw, "node-a")[0]
    warnings: list[str] = []
    service._correlate(raw, ParsedSSHData(), warnings, selected_enclosure_id="node-a")
    admitted = record.raw.get("quantastor_pool_device")
    return (admitted.get("id") if admitted else None, bool(record.raw.get("quantastor_pool_device_conflict")),
            [warning for warning in warnings if warning.startswith("QuantaStor gave conflicting")])


ADMITTED = ("member-a", False, [])
UNKNOWN = (None, True, [CONFLICT_WARNING_ONE])


class QuantastorDiskIdentityIndexTests(unittest.TestCase):
    def test_hardware_rows_that_share_a_key_are_each_an_identity_source(self) -> None:
        # Several hardware rows carry the disk's physical ID. Each is coherent
        # alone; together they report two serials, in any order.
        import itertools

        disk = IDENTITY_DISK
        rows = [_hw("hw-1", physicalDiskId="disk-a", serialNumber=SERIAL_A),
                _hw("hw-2", physicalDiskId="disk-a", serialNumber=SERIAL_C),
                _hw("hw-3", physicalDiskId="disk-a", serialNumber=SERIAL_A)]
        # Every serial spelling a hardware row can carry counts.
        for field in ("serialNumber", "serialNum", "serial"):
            with self.subTest(spelling=field):
                self.assertEqual(_membership(disk, hw=[rows[0], _hw("hw-2", physicalDiskId="disk-a",
                                                                    **{field: SERIAL_C})]), UNKNOWN)
        for row in rows:
            with self.subTest(single=row["id"]):
                self.assertEqual(_membership(disk, hw=[row]), ADMITTED)
        for order in itertools.permutations(rows):
            with self.subTest(order=[row["id"] for row in order]):
                self.assertEqual(_membership(disk, hw=order), UNKNOWN)
        # A row naming one of the two serials is not admitted either.
        named = {**PATH_ROW, "physicalDiskSerialNumber": SERIAL_A}
        self.assertEqual(_membership(disk, hw=[rows[0]], pool_devices=[named]), ADMITTED)
        for order in (rows[:2], rows[1::-1]):
            with self.subTest(named_order=[row["id"] for row in order]):
                self.assertEqual(_membership(disk, hw=order, pool_devices=[named]), UNKNOWN)

    def test_a_blank_primary_serial_does_not_hide_the_other_serial_alias(self) -> None:
        # The hardware row is reachable only by the second REST alias, and it
        # also reports another serial.
        row = _hw("hw-serial-only", serialNum=SERIAL_A, serialNumber=SERIAL_C)
        control = _hw("hw-serial-only", serialNum=SERIAL_A)
        for blank in ("", "   ", " \t "):
            with self.subTest(primary=repr(blank)):
                disk = {**IDENTITY_DISK, "serialNumber": blank, "serial": SERIAL_A}
                self.assertEqual(_membership(disk, hw=[control]), ADMITTED)
                self.assertEqual(_membership(disk, hw=[row]), UNKNOWN)

    def test_a_hardware_row_without_a_slot_is_still_an_identity_source(self) -> None:
        disk = {**IDENTITY_DISK, "serialNumber": SERIAL_A}
        slotless = {key: value for key, value in _hw("hw-1", physicalDiskId="disk-a").items() if key != "slot"}
        self.assertEqual(_membership(disk, hw=[{**slotless, "serialNumber": SERIAL_A}]), ADMITTED)
        self.assertEqual(_membership(disk, hw=[{**slotless, "serialNumber": SERIAL_C}]), UNKNOWN)

    def test_api_hardware_rows_are_not_hidden_by_cli_hardware_rows(self) -> None:
        # Both arrays are populated; the CLI array still places the bay, but
        # the API rows are identity sources too.
        disk = {**IDENTITY_DISK, "serialNumber": SERIAL_A}
        agree = _hw("hw-1", physicalDiskId="disk-a", serialNumber=SERIAL_A)
        differ = _hw("hw-1", physicalDiskId="disk-a", serialNumber=SERIAL_C)
        self.assertEqual(_membership(disk, hw=[agree]), ADMITTED)
        self.assertEqual(_membership(disk, cli_hw=[agree]), ADMITTED)
        self.assertEqual(_membership(disk, hw=[agree], cli_hw=[agree]), ADMITTED)
        self.assertEqual(_membership(disk, hw=[differ], cli_hw=[agree]), UNKNOWN)
        self.assertEqual(_membership(disk, hw=[agree], cli_hw=[differ]), UNKNOWN)

    def test_a_hardware_row_reached_only_by_its_scsi_id_is_an_identity_source(self) -> None:
        disk = {**IDENTITY_DISK, "serialNumber": SERIAL_A, "scsiId": "scsi-synthetic-a"}
        self.assertEqual(_membership(disk, hw=[_hw("hw-scsi", scsiId="scsi-synthetic-a", serialNumber=SERIAL_A)]),
                         ADMITTED)
        self.assertEqual(_membership(disk, hw=[_hw("hw-scsi", scsiId="scsi-synthetic-a", serialNumber=SERIAL_C)]),
                         UNKNOWN)

    def test_hardware_groups_with_different_sas_addresses_are_all_read(self) -> None:
        disk = {**IDENTITY_DISK, "serialNumber": SERIAL_A}
        first = _hw("hw-1", physicalDiskId="disk-a", serialNumber=SERIAL_A, sasAddress="sas-synthetic-101")
        second = _hw("hw-2", serialNumber=SERIAL_A, sasAddress="sas-synthetic-202")
        self.assertEqual(_membership(disk, hw=[first, {**second, "serialNum": SERIAL_A}]), ADMITTED)
        for order in ([first, {**second, "serialNum": SERIAL_C}], [{**second, "serialNum": SERIAL_C}, first]):
            with self.subTest(order=[row["id"] for row in order]):
                self.assertEqual(_membership(disk, hw=order), UNKNOWN)
        # Reached only through another row's key (its SAS address): the walk
        # continues until nothing new is reached.
        linked = _hw("hw-1", physicalDiskId="disk-a", sasAddress="sas-synthetic-101")
        for serial, expected in ((SERIAL_A, ADMITTED), (SERIAL_C, UNKNOWN)):
            behind = _hw("hw-behind", sasAddress="sas-synthetic-101", serialNumber=serial)
            for order in ([linked, behind], [behind, linked]):
                with self.subTest(behind=serial, order=[row["id"] for row in order]):
                    self.assertEqual(_membership(disk, hw=order), expected)

    def test_an_identity_set_without_any_serial_is_decided_by_the_other_identities(self) -> None:
        # No source reports a serial. That is absence, not disagreement: the
        # SCSI ID and the same-node physical ID still decide, and must agree.
        disk = {**IDENTITY_DISK, "scsiId": "scsi-synthetic-a"}
        self.assertEqual(_membership(disk), ADMITTED)
        self.assertEqual(_membership(disk, hw=[_hw("hw-1", physicalDiskId="disk-a", scsiId="scsi-synthetic-a")]),
                         ADMITTED)
        self.assertEqual(_membership(disk, hw=[_hw("hw-1", physicalDiskId="disk-a", scsiId="scsi-synthetic-c")]),
                         UNKNOWN)
        self.assertEqual(_membership(disk, hw=[_hw("hw-1", serialNumber=None, physicalDiskId="disk-c",
                                                   scsiId="scsi-synthetic-a")]), UNKNOWN)


# Whole-set generator, cut down from an independent review. The oracle below is
# written from the rule and never calls the service: it lists which fixture
# rows are linked to the disk and counts the distinct serials they report.
WS_A = "SYNTHETIC-WS-A"
WS_B = "SYNTHETIC-WS-B"
WS_SAS = "sas-synthetic-ws"
WS_CANDIDATE = {"id": "ws-row", "storageSystemId": "node-a", "storagePoolId": "pool-a", "devicePath": "/dev/ws-a",
                "slot": 1, "raidGroupId": "ws-vdev", "status": "ONLINE"}
WS_DECOY = {"id": "ws-decoy", "storageSystemId": "node-b", "storagePoolId": "pool-a", "devicePath": "/dev/ws-a",
            "slot": 1, "physicalDiskSerialNumber": "SYNTHETIC-WS-FOREIGN"}
WS_COUNTS = {"cases": 4374, "admitted": 636, "conflict": 2280, "no-candidate": 1458}


def _whole_set_fixture(rest, layout, hw_values, cli_serial):
    """REST disk, four linked hardware rows (each reached another way) and one unlinked row."""
    disk = {"id": "ws-disk", "storageSystemId": "node-a", "enclosureId": "shelf-a", "devicePath": "/dev/ws-a",
            "slot": 0, "healthStatus": "ONLINE", "scsiId": "ws-scsi"}
    if layout == "blank-primary":
        disk["serialNumber"] = "  "
    if rest is not None:
        disk["serial" if layout == "blank-primary" else "serialNumber"] = rest
    v0, v1, v2, v3 = hw_values
    base = {"storageSystemId": "node-a", "enclosureId": "shelf-a"}
    api = [
        # Linked by the disk's ID; carries a SAS address.
        {**base, "id": "ws-hw-0", "slot": 0, "physicalDiskId": "ws-disk", "sasAddress": WS_SAS, "serialNumber": v0},
        # Linked by the disk's SCSI ID only.
        {**base, "id": "ws-hw-2", "slot": 0, "scsiId": "ws-scsi", "serialNumber": v2},
        # Linked only through ws-hw-0's SAS address.
        {**base, "id": "ws-hw-3", "slot": 0, "sasAddress": WS_SAS, "serialNum": v3},
        # Shares no key with the disk or the rows above; its serial must never count.
        {**base, "id": "ws-hw-unlinked", "slot": 1, "physicalDiskId": "ws-other", "serialNumber": "SYNTHETIC-WS-U"},
    ]
    # Slotless, in the CLI array, linked by the disk's ID; absent when v1 is None.
    cli_hw = [] if v1 is None else [{**base, "id": "ws-hw-1", "physicalDiskId": "ws-disk", "serialNum": v1}]
    cli = {"physicalDiskId": "ws-disk", "storageSystemId": "node-a", "devicePath": "/dev/ws-a"}
    if cli_serial is not None:
        cli["serial"] = cli_serial
    return disk, api, cli_hw, cli


def _whole_set_expectation(rest, hw_values, cli_serial, with_candidate):
    """(admitted, conflict) from the rule, without the service."""
    # The CLI disk list row is matched to the REST disk unless their serials differ.
    cli_matched = not (rest and cli_serial and rest != cli_serial)
    serials = {value for value in (rest, *hw_values, cli_serial if cli_matched else None) if value}
    if not with_candidate:
        return False, False
    return (False, True) if len(serials) > 1 else (True, False)


class QuantastorDiskIdentityWholeSetTests(unittest.TestCase):
    def test_every_source_combination_against_an_independent_oracle(self) -> None:
        import itertools

        service = _service()
        counts = {"cases": 0, "admitted": 0, "conflict": 0, "no-candidate": 0}
        mismatches = []
        for rest, layout, *rest_of_state in itertools.product(
            (None, WS_A, WS_B), ("plain", "blank-primary"), *[(None, WS_A, WS_B)] * 5,
        ):
            hw_values, cli_serial = tuple(rest_of_state[:4]), rest_of_state[4]
            disk, api, cli_hw, cli = _whole_set_fixture(rest, layout, hw_values, cli_serial)
            for with_candidate, order in ((True, 1), (True, -1), (False, 1)):
                pool_rows = [WS_CANDIDATE, WS_DECOY] if with_candidate else [WS_DECOY]
                raw = _raw(disks=[disk], pools=[HOME_POOL], pool_devices=pool_rows[::order])
                raw.hw_disks = api[::order]
                raw.cli_hw_disks = cli_hw
                raw.cli_disks = [cli]
                record = service._build_quantastor_disk_records(raw, "node-a")[0]
                admitted_row = record.raw.get("quantastor_pool_device")
                got = (admitted_row is WS_CANDIDATE, bool(record.raw.get("quantastor_pool_device_conflict")))
                expected = _whole_set_expectation(rest, hw_values, cli_serial, with_candidate)
                if got != expected:
                    mismatches.append((rest, layout, hw_values, cli_serial, with_candidate, order, got, expected))
                counts["cases"] += 1
                counts["conflict" if expected[1] else "admitted" if expected[0] else "no-candidate"] += 1
        if mismatches:
            shown = "\n".join(repr(item) for item in mismatches[:12])
            self.fail(f"{len(mismatches)} of {counts['cases']} generated cases differ from the rule:\n{shown}")
        # Every outcome is reached, so no branch of the rule is vacuous.
        self.assertEqual(counts, WS_COUNTS)



if __name__ == "__main__":
    unittest.main()
