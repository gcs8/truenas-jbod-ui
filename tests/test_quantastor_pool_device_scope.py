"""QuantaStor pool-device rows: device paths stay on their own node (#1211).

Synthetic fixtures only. Two nodes report different disks at the same local
path (/dev/sda). Node B's pool-device row must not move node A's disk to
another bay or give it node B's pool, whatever the input order. Stable
identities (physical disk ID, serial, SCSI ID) still match across nodes, so an
HA peer's row for a shared disk is admitted. Rows are otherwise looked up as
before: the guards at the end pin shapes that must keep their rows.
"""

from __future__ import annotations

import itertools
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


def _service(bays: int = 2) -> InventoryService:
    profile = EnclosureProfileConfig(
        id=f"synthetic-{bays}-bay", label="Synthetic bays", rows=1, columns=bays, slot_layout=[list(range(bays))],
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
        layout=LayoutConfig(slot_count=bays, rows=1, columns=bays, api_slot_number_base=0),
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
HOME_POOL = {"id": "pool-a", "name": "HOME-POOL", "storageSystemId": "node-a"}
TWO_NODES = ({"id": "node-a"}, {"id": "node-b"})


def _raw(*, disks, pools=(), pool_devices=(), systems=TWO_NODES) -> TrueNASRawData:
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
        # The second foreign row names nothing but its path, so only the node
        # scope of the path keeps it off node A's disk.
        path_only = {**MEMBER_B, "physicalDiskId": None, "physicalDiskSerialNumber": None}
        for order, member in itertools.product((1, -1), (MEMBER_B, path_only)):
            with self.subTest(order=order, serial=member["physicalDiskSerialNumber"]):
                raw = _raw(disks=[DISK_A, DISK_B][::order], pools=[POOL_B], pool_devices=[member])
                records = service._build_quantastor_disk_records(raw, "node-a")
                self.assertEqual([(record.serial, record.slot) for record in records], [("SANITIZED-QS-A", 0)])
                self.assertNotIn("quantastor_pool_device", records[0].raw)
                self.assertEqual(_bays(service, raw), self.HOME)

    def test_own_node_row_at_the_local_path_is_still_admitted(self) -> None:
        # A row from the disk's own node at its path keeps placement and topology,
        # whatever the order of a foreign row at the same local path.
        service = _service()
        own_row = {
            "id": "member-a", "storageSystemId": "node-a", "storagePoolId": "pool-a",
            "devicePath": "/dev/sda", "slot": 1, "raidGroupId": "home-vdev", "status": "ONLINE",
        }
        for pool_devices in ([own_row], [own_row, MEMBER_B], [MEMBER_B, own_row]):
            with self.subTest(rows=[row["id"] for row in pool_devices]):
                raw = _raw(disks=[DISK_A, DISK_B], pools=[HOME_POOL, POOL_B], pool_devices=pool_devices)
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

    def test_a_peer_disk_at_the_same_local_path_does_not_make_this_disk_visible_there(self) -> None:
        service = _service()
        context = {"cluster_node_ids": ["node-a", "node-b"], "selected_view_label": "node-a"}
        cli_b = {"id": "cli-b", "storageSystemId": "node-b", "devicePath": "/dev/sda", "serialNumber": "SANITIZED-QS-B"}
        cases = [
            # Distinct disks that share only /dev/sda: visible on node A alone.
            ([DISK_A, DISK_B], [], [], ["node-a"]),
            # The foreign row at that path adds nothing either.
            ([DISK_A, DISK_B], [{**MEMBER_B, "physicalDiskId": None, "physicalDiskSerialNumber": None}], [],
             ["node-a"]),
            # Nor does node B's CLI disk row at that path.
            ([DISK_A], [], [cli_b], ["node-a"]),
            # Control: the peer reports the same serial, so the disk is shared.
            ([DISK_A, {**DISK_B, "serialNumber": "SANITIZED-QS-A"}], [], [], ["node-a", "node-b"]),
            # Control: the peer's row names the shared disk at the peer's own path.
            ([DISK_A, {**DISK_B, "id": "peer-disk-a", "devicePath": "/dev/sdq", "serialNumber": None}],
             [self.PEER_ROW], [], ["node-a", "node-b"]),
        ]
        for disks, pool_devices, cli_disks, visible_on in cases:
            with self.subTest(visible_on=visible_on, rows=len(pool_devices), cli=len(cli_disks)):
                raw = _raw(disks=disks, pools=[POOL_B], pool_devices=pool_devices)
                raw.cli_disks = list(cli_disks)
                slots, *_ = service._correlate(raw, ParsedSSHData(), [], selected_enclosure_id="node-a")
                service._annotate_quantastor_slot_context(slots, raw, "node-a", context)
                bay = next(slot for slot in slots if slot.present)
                self.assertEqual(bay.serial, "SANITIZED-QS-A")
                self.assertEqual(bay.operator_context["visible_on_labels"], visible_on)

    def test_a_row_with_no_known_node_files_no_path_on_two_nodes_and_does_on_one(self) -> None:
        # Which node's /dev/sda a node-less row means is unknown on a grid of
        # two nodes; on a single node there is only one answer.
        service = _service()
        no_node = {
            "id": "member-x", "storagePoolId": "pool-a", "devicePath": "/dev/sda", "slot": 1,
            "raidGroupId": "home-vdev", "status": "ONLINE",
        }
        for systems, admitted in ((TWO_NODES, False), (({"id": "node-a"},), True)):
            for disks in ([DISK_A, DISK_B], [DISK_B, DISK_A]):
                with self.subTest(nodes=len(systems), first=disks[0]["id"]):
                    raw = _raw(disks=disks, pools=[HOME_POOL], pool_devices=[no_node], systems=systems)
                    record = service._build_quantastor_disk_records(raw, "node-a")[0]
                    self.assertEqual(record.raw.get("quantastor_pool_device"), no_node if admitted else None)
                    self.assertEqual(
                        _bays(service, raw),
                        [
                            (0, None, False, None, None, None),
                            (1, "SANITIZED-QS-A", True, "HOME-POOL", "home-vdev",
                             "HOME-POOL > home-vdev > data (active on node-a)"),
                        ] if admitted else self.HOME,
                    )


SERIAL_A = "SANITIZED-QS-A"
IDENTITY_DISK = {"id": "disk-a", "storageSystemId": "node-a", "enclosureId": "shelf-a", "devicePath": "/dev/sda",
                 "slot": 0, "healthStatus": "ONLINE"}
# Reached by the own-node path only: it names no identity of its own.
PATH_ROW = {"id": "member-a", "storageSystemId": "node-a", "storagePoolId": "pool-a", "devicePath": "/dev/sda",
            "slot": 1, "raidGroupId": "vdev-a", "status": "ONLINE"}


def _admitted(disk, *, hw=(), pool_devices=(PATH_ROW,), cli=(), expect_cli=None):
    """The id of the pool-device row the shipping builder gives `disk`, if any."""
    service = _service()
    raw = _raw(disks=[disk], pools=[HOME_POOL], pool_devices=list(pool_devices))
    raw.hw_disks = list(hw)
    raw.cli_disks = list(cli)
    record = service._build_quantastor_disk_records(raw, "node-a")[0]
    if expect_cli is not None:
        # Fixture self-check: the CLI disk row really was matched to the disk.
        assert record.raw.get("quantastor_cli_disk") == expect_cli, record.raw.get("quantastor_cli_disk")
    admitted = record.raw.get("quantastor_pool_device")
    return admitted.get("id") if admitted else None


class QuantastorPoolDeviceLookupGuards(unittest.TestCase):
    """Shapes the lookup already handles; node scoping must not change them."""

    def test_hardware_row_reads_grow_with_the_input_not_with_disks_times_rows(self) -> None:
        # Each disk has its own hardware row among as many unrelated rows and
        # keeps the row its own path names; reads grow with the input.
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

    def test_a_serial_only_the_cli_row_reports_is_not_a_pool_device_lookup_key(self) -> None:
        # A pool-device row that only the CLI disk row's serial (or a hardware
        # row reached only through it) would find is not the disk's row.
        cli_row = {**IDENTITY_DISK, "serialNumber": SERIAL_A}
        by_serial = {**PATH_ROW, "id": "member-by-serial", "devicePath": "/dev/sdz",
                     "physicalDiskSerialNumber": SERIAL_A}
        by_hw_id = {**PATH_ROW, "id": "member-by-hw", "devicePath": "/dev/sdz",
                    "physicalDiskId": "hw-reached-by-cli"}
        reached = {"id": "hw-reached-by-cli", "storageSystemId": "node-a", "enclosureId": "shelf-a",
                   "serialNum": SERIAL_A}
        for pool_devices in ([by_serial], [by_hw_id]):
            for hw in ([], [reached]):
                with self.subTest(row=pool_devices[0]["id"], hw=len(hw)):
                    self.assertIsNone(_admitted(IDENTITY_DISK, hw=hw, cli=[cli_row], expect_cli=cli_row,
                                                pool_devices=pool_devices))
        # Control: the same serial on the REST row is a lookup key.
        self.assertEqual(_admitted({**IDENTITY_DISK, "serialNumber": SERIAL_A}, pool_devices=[by_serial]),
                         "member-by-serial")

    def test_disks_sharing_a_hardware_serial_keep_their_path_rows_with_linear_reads(self) -> None:
        # Groups of disks share one serial with 256 hardware rows each; every
        # disk keeps the row its own path names, and twice the groups cost
        # about twice the reads.
        service = _service()

        def run(groups, disks_per_group=8):
            reads = [0]
            disks, pool_rows, hw = [], [], []
            for group in range(groups):
                serial = f"SANITIZED-QS-G{group}"
                hw += [{"id": f"hw-{group}-{n}", "storageSystemId": "node-a", "enclosureId": "shelf-a",
                        "serialNum": serial, "slot": 0} for n in range(256)]
                for n in range(disks_per_group):
                    path = f"/dev/synthetic-{group}-{n}"
                    disks.append({**IDENTITY_DISK, "id": f"disk-{group}-{n}", "devicePath": path,
                                  "serialNumber": serial})
                    pool_rows.append({**PATH_ROW, "id": f"member-{group}-{n}", "devicePath": path})
            raw = _raw(disks=disks, pools=[HOME_POOL], pool_devices=pool_rows)
            raw.hw_disks = _counted_rows(hw, reads)
            records = service._build_quantastor_disk_records(raw, "node-a")
            self.assertEqual([record.raw.get("quantastor_pool_device") for record in records], pool_rows)
            return reads[0]

        reads = run(2)
        self.assertLess(run(4), 3 * reads)

    def _two_disks(self, *, disk_fields=lambda n: {}, row_fields=lambda n: {}):
        disks = [{"id": f"disk-{n}", "storageSystemId": "node-a", "enclosureId": "shelf-a",
                  "devicePath": f"/dev/sd{'bc'[n]}", "serialNumber": f"SER-{n}", "scsiId": f"scsi-{n}",
                  "healthStatus": "ONLINE", **disk_fields(n)} for n in range(2)]
        pools = [{"id": f"pool-{n}", "name": f"POOL-{n}", "storageSystemId": "node-a"} for n in range(2)]
        rows = [{"id": f"member-{n}", "storageSystemId": "node-a", "storagePoolId": f"pool-{n}",
                 "physicalDiskId": f"disk-{n}", "physicalDiskSerialNumber": f"SER-{n}",
                 "physicalDiskScsiId": f"scsi-{n}", "devicePath": f"/dev/sd{'bc'[n]}", "slot": n,
                 "raidGroupId": f"vdev-{n}", "status": "ONLINE", **row_fields(n)} for n in range(2)]
        return disks, pools, rows

    def _assert_own_rows(self, disks, pools, rows):
        service = _service()
        expected = [(n, f"SER-{n}", True, f"POOL-{n}", f"vdev-{n}") for n in range(2)]
        for disk_order, row_order in itertools.product((1, -1), (1, -1)):
            with self.subTest(disk_order=disk_order, row_order=row_order):
                raw = _raw(disks=disks[::disk_order], pools=pools, pool_devices=rows[::row_order])
                records = service._build_quantastor_disk_records(raw, "node-a")
                self.assertEqual(
                    {record.raw["id"]: (record.raw.get("quantastor_pool_device") or {}).get("id") for record in records},
                    {disk["id"]: f"member-{disk['id'][-1]}" for disk in disks},
                )
                self.assertEqual([bay[:5] for bay in _bays(service, raw)], expected)

    def test_a_pool_serial_that_differs_only_in_case_keeps_its_row(self) -> None:
        disks, pools, rows = self._two_disks(row_fields=lambda n: {"physicalDiskSerialNumber": f"ser-{n}"})
        self._assert_own_rows(disks, pools, rows)

    def test_rows_sharing_a_physical_disk_id_placeholder_keep_the_rows_their_paths_name(self) -> None:
        disks, pools, rows = self._two_disks(row_fields=lambda n: {"physicalDiskId": "0"})
        self._assert_own_rows(disks, pools, rows)

    def test_a_blank_first_serial_alias_keeps_every_records_row(self) -> None:
        # One disk reported as raw, multipath and crypt records; the first
        # serial alias is blank and the real serial is in the second.
        service = _service()
        for named in ("raw", "mp", "crypt"):
            disks, pools, rows = [], [], []
            for n in range(2):
                for kind, path in (("raw", f"/dev/sd{'bc'[n]}"), ("mp", f"/dev/dm-{n}"),
                                   ("crypt", f"/dev/mapper/crypt-{n}")):
                    disks.append({"id": f"{kind}-{n}", "storageSystemId": "node-a", "enclosureId": "shelf-a",
                                  "devicePath": path, "serialNumber": "   ", "serial": f"SER-{n}",
                                  "scsiId": f"scsi-{n}", "healthStatus": "ONLINE"})
                pools.append({"id": f"pool-{n}", "name": f"POOL-{n}", "storageSystemId": "node-a"})
                rows.append({"id": f"member-{n}", "storageSystemId": "node-a", "storagePoolId": f"pool-{n}",
                             "physicalDiskId": f"{named}-{n}", "physicalDiskSerialNumber": f"SER-{n}",
                             "physicalDiskScsiId": f"scsi-{n}", "slot": n, "raidGroupId": f"vdev-{n}",
                             "status": "ONLINE"})
            for disk_order, row_order in itertools.product((1, -1), (1, -1)):
                with self.subTest(named=named, disk_order=disk_order, row_order=row_order):
                    raw = _raw(disks=disks[::disk_order], pools=pools, pool_devices=rows[::row_order])
                    records = service._build_quantastor_disk_records(raw, "node-a")
                    self.assertEqual(
                        sorted((record.raw["id"], (record.raw.get("quantastor_pool_device") or {}).get("id"))
                               for record in records),
                        sorted((disk["id"], f"member-{disk['id'][-1]}") for disk in disks),
                    )
                    self.assertEqual([bay[3:5] for bay in _bays(service, raw)],
                                     [("POOL-0", "vdev-0"), ("POOL-1", "vdev-1")])


if __name__ == "__main__":
    unittest.main()
