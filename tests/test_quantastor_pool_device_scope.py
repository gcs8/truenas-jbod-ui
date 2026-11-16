"""QuantaStor pool-device rows: scoped paths, no shared keys (#1211).

Synthetic fixtures only. Two nodes report different disks at the same local
path (/dev/sda). Node B's pool-device row must not move node A's disk to
another bay or give it node B's pool, whatever the input order. A value that
several disks carry (a placeholder serial, for example) identifies none of
them: it must not give a disk another disk's row, and it must not cost a disk
the row its own path or physical disk ID names.
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

    def test_every_record_of_one_disk_keeps_the_row_that_names_one_of_them(self) -> None:
        # One disk reported as its raw, multipath and crypt records (same node,
        # serial and SCSI ID, three IDs). The row names the raw record's ID; it
        # is still this disk's row for the other two records, in either order.
        service = _service()
        records = [{**DISK_A, "id": f"disk-a{suffix}", "devicePath": path, "scsiId": "LUN-A"}
                   for suffix, path in (("", "/dev/sda"), ("-mp", "/dev/dm-3"), ("-cr", "/dev/dm-12"))]
        row = {"id": "member-a", "storageSystemId": "node-a", "storagePoolId": "pool-a", "devicePath": "/dev/sda",
               "slot": 0, "physicalDiskId": "disk-a", "physicalDiskSerialNumber": "SANITIZED-QS-A",
               "physicalDiskScsiId": "LUN-A"}
        for order, physical_id, expected in ((1, "disk-a", "member-a"), (-1, "disk-a", "member-a"),
                                             (1, "disk-z", None)):
            with self.subTest(order=order, physical_id=physical_id):
                raw = _raw(disks=records[::order], pools=[HOME_POOL],
                           pool_devices=[{**row, "physicalDiskId": physical_id}])
                self.assertEqual(
                    sorted((record.raw["id"], (record.raw.get("quantastor_pool_device") or {}).get("id"))
                           for record in service._build_quantastor_disk_records(raw, "node-a")),
                    sorted((record["id"], expected) for record in records),
                )

    def test_a_key_many_disks_share_admits_no_row_and_is_not_read_per_disk(self) -> None:
        # 200 disks on node A share one serial, and so do 200 peer rows naming
        # 200 different physical disks. The serial names none of them, so no
        # disk takes a row, each is reported, and the rows behind the shared
        # key are not examined disk by disk (work counted in rows checked).
        from unittest import mock

        service = _service()
        disks = [{**DISK_A, "id": f"disk-{index}", "devicePath": f"/dev/sd{index}"} for index in range(200)]
        rows = [{**self.PEER_ROW, "id": f"member-{index}", "physicalDiskId": f"peer-{index}"} for index in range(200)]
        raw = _raw(disks=disks, pools=[POOL_B], pool_devices=rows)
        checked = mock.Mock(wraps=InventoryService._quantastor_pool_device_contradicts)
        with mock.patch.object(InventoryService, "_quantastor_pool_device_contradicts", checked):
            records = service._build_quantastor_disk_records(raw, "node-a")
        self.assertEqual(len(records), 200)
        self.assertEqual(sum("quantastor_pool_device" in record.raw for record in records), 0)
        self.assertEqual(sum(bool(record.raw.get("quantastor_pool_device_conflict")) for record in records), 200)
        self.assertEqual(checked.call_count, 0)

        # Control: one disk and one peer row, the same serial: the row is admitted.
        raw = _raw(disks=[DISK_A], pools=[POOL_B], pool_devices=[self.PEER_ROW])
        self.assertIs(service._build_quantastor_disk_records(raw, "node-a")[0].raw["quantastor_pool_device"],
                      self.PEER_ROW)

    def test_a_shared_serial_does_not_cost_a_disk_the_row_its_own_path_names(self) -> None:
        # Every disk row and every pool-device row carries the same placeholder
        # serial; each row also names its own disk's path or physical disk ID.
        # Every disk keeps its own row, without a warning, in either order, and
        # each disk checks a bounded number of rows however many disks there are.
        from unittest import mock

        def run(count, keys, order):
            service = _service()
            disks = [{**DISK_A, "id": f"disk-{n}", "devicePath": f"/dev/sd{n}", "serialNumber": "0"}
                     for n in range(count)]
            rows = [{"id": f"member-{n}", "storageSystemId": "node-a", "storagePoolId": "pool-a", "slot": n,
                     "raidGroupId": f"vdev-{n}", "status": "ONLINE", "physicalDiskSerialNumber": "0",
                     **({"physicalDiskId": f"disk-{n}"} if "id" in keys else {}),
                     **({"devicePath": f"/dev/sd{n}"} if "path" in keys else {})} for n in range(count)]
            raw = _raw(disks=disks[::order], pools=[HOME_POOL], pool_devices=rows[::order])
            checked = mock.Mock(wraps=InventoryService._quantastor_pool_device_contradicts)
            with mock.patch.object(InventoryService, "_quantastor_pool_device_contradicts", checked):
                records = service._build_quantastor_disk_records(raw, "node-a")
            members = sorted((record.raw["id"], (record.raw.get("quantastor_pool_device") or {}).get("id"),
                              bool(record.raw.get("quantastor_pool_device_conflict"))) for record in records)
            return members, checked.call_count

        for keys in (("id",), ("path",), ("id", "path")):
            for order in (1, -1):
                with self.subTest(keys=keys, order=order):
                    members, checks = run(100, keys, order)
                    self.assertEqual(members, sorted((f"disk-{n}", f"member-{n}", False) for n in range(100)))
                    self.assertLessEqual(checks, 2 * 100)
        # Control: with neither, the shared serial is all that links disks to
        # rows, so every membership is withheld and reported.
        members, _checks = run(3, (), 1)
        self.assertEqual(members, [(f"disk-{n}", None, True) for n in range(3)])


# Generated shapes (#1211). Ground truth is by construction: on each node every
# disk has exactly one own pool-device row. One value is carried by every disk
# in one identity field, or in all of them at once. Expectations are written
# from the rule, not from the code: a disk whose own row is named by its own
# path, its own physical disk ID, or a serial or SCSI ID no other disk carries
# gets that row, its bay, pool and vdev, and no warning. No disk ever gets
# another disk's row. The membership never depends on the input order.
SHARED_VALUES = ("0", "00000000", "N/A", "SYNTHETIC-SHARED")
SHARED_FIELDS = ("disk_serial", "hw_serial", "hw_sas", "hw_scsi", "pool_serial", "pool_scsi")
GENERATED_COUNTS = {"evaluations": 928, "own-row": 3360, "withheld": 120}


def _generated_raw(nodes, count, value, fields, with_id, with_path, reverse):
    disks, hw, rows = [], [], []
    for node in nodes:
        for n in range(count):
            # Node B's /dev/sdN is another disk than node A's /dev/sdN.
            local = n if node == "node-a" else (n + 1) % count
            disks.append({"id": f"{node}-d{n}", "storageSystemId": node, "enclosureId": "shelf-a",
                          "devicePath": f"/dev/sd{local}", "scsiId": f"scsi-{n}", "healthStatus": "ONLINE",
                          "serialNumber": value if "disk_serial" in fields else f"SER-{n}"})
            hw.append({"id": f"hw-{node}-{n}", "storageSystemId": node, "enclosureId": "shelf-a", "slot": n,
                       "physicalDiskId": f"{node}-d{n}",
                       "serialNumber": value if "hw_serial" in fields else f"SER-{n}",
                       "sasAddress": value if "hw_sas" in fields else f"5000c500{n:08x}",
                       "scsiId": value if "hw_scsi" in fields else f"scsi-{n}"})
            rows.append({"id": f"member-{node}-{n}", "storageSystemId": node, "storagePoolId": f"pool-{n % 2}",
                         "slot": n, "raidGroupId": f"vdev-{n}", "status": "ONLINE",
                         "physicalDiskSerialNumber": value if "pool_serial" in fields else f"SER-{n}",
                         "physicalDiskScsiId": value if "pool_scsi" in fields else f"scsi-{n}",
                         **({"physicalDiskId": f"{node}-d{n}"} if with_id else {}),
                         **({"devicePath": f"/dev/sd{local}"} if with_path else {})})
    order = -1 if reverse else 1
    raw = _raw(disks=disks[::order], pool_devices=rows[::order], systems=[{"id": node} for node in nodes[::order]],
               pools=[{"id": f"pool-{n}", "name": f"POOL-{n}", "storageSystemId": "node-a"} for n in range(2)])
    raw.hw_disks = hw[::order]
    return raw


class QuantastorSharedValueGeneratedTests(unittest.TestCase):
    def test_a_shared_value_in_any_identity_field_never_gives_another_disks_row(self) -> None:
        placements = [frozenset(), *(frozenset([name]) for name in SHARED_FIELDS), frozenset(SHARED_FIELDS)]
        counts = {"evaluations": 0, "own-row": 0, "withheld": 0}
        problems = []
        for nodes, count, value, fields, with_id, with_path in itertools.product(
            (["node-a"], ["node-a", "node-b"]), (2, 3), SHARED_VALUES, placements, (True, False), (True, False),
        ):
            if not fields and value != SHARED_VALUES[0]:
                continue
            # The rule: own path, own physical disk ID, or an unshared serial or SCSI ID.
            named = (with_id or with_path or not fields & {"disk_serial", "pool_serial"}
                     or "pool_scsi" not in fields)
            service = _service(bays=count)
            memberships = []
            for reverse in (False, True):
                counts["evaluations"] += 1
                raw = _generated_raw(nodes, count, value, fields, with_id, with_path, reverse)
                membership = {}
                for node in nodes:
                    warnings: list[str] = []
                    slots, *_ = service._correlate(raw, ParsedSSHData(), warnings, selected_enclosure_id=node)
                    bays = {slot.slot: slot for slot in slots}
                    for record in service._build_quantastor_disk_records(raw, node):
                        n = int(record.raw["id"].rsplit("-d", 1)[1])
                        member = (record.raw.get("quantastor_pool_device") or {}).get("id")
                        membership[record.raw["id"]] = member
                        bay = bays[n]
                        own = (member == f"member-{node}-{n}" and record.slot == n
                               and (bay.pool_name, bay.vdev_name) == (f"POOL-{n % 2}", f"vdev-{n}"))
                        withheld = member is None and bay.pool_name is None
                        counts["own-row" if own else "withheld" if withheld else "wrong"] = (
                            counts.get("own-row" if own else "withheld" if withheld else "wrong", 0) + 1)
                        if not own and (named or not withheld):
                            problems.append((nodes, count, value, sorted(fields), with_id, with_path, reverse,
                                             node, record.raw["id"], member))
                    if named and any(warning.startswith("QuantaStor gave conflicting") for warning in warnings):
                        problems.append((nodes, count, value, sorted(fields), with_id, with_path, reverse,
                                         node, "warning"))
                memberships.append(membership)
            if memberships[0] != memberships[1]:
                problems.append((nodes, count, value, sorted(fields), with_id, with_path, "order"))
        self.assertEqual(problems[:5], [])
        self.assertEqual(counts, GENERATED_COUNTS)
