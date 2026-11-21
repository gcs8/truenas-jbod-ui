"""QuantaStor SMART admission: unsigned readings and node-local SMART paths.

Synthetic fixtures only; no appliance payload or SMART command is used.

#1299: a negative value for an unsigned SMART measurement (block size, rotation
rate, endurance, error counters, byte totals) is not a reading. It stays
unknown and is named in ``unread_fields``; legitimate zeroes and negative
Celsius temperatures stay valid.

#1300: a negative power-on time between -1 and 0 (number or text) is refused
before conversion to whole hours, so it never becomes a measured zero.

#1318: a pool-device row from a peer node can be admitted for the selected
node's disk through a stable shared identity, but its node-local device path
never becomes a SMART candidate on the selected node.
"""

from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path

from app.config import (
    EnclosureProfileConfig,
    LayoutConfig,
    PathConfig,
    Settings,
    SystemConfig,
    TrueNASConfig,
)
from app.models.domain import SlotView, SmartSummaryView
from app.services.inventory import InventoryService
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
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
        id="synthetic-2-bay", label="Synthetic bays", rows=1, columns=2, slot_layout=[[0, 1]],
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


def _summary(service: InventoryService, disk_raw: dict, **slot_fields):
    slot = SlotView(
        slot=0, slot_label="00", row_index=0, column_index=0, present=True,
        raw_status={"disk_raw": disk_raw}, **slot_fields,
    )
    return service._build_quantastor_smart_summary(slot)


DISK_A = {
    "id": "disk-a", "storageSystemId": "node-a", "enclosureId": "shelf-a", "devicePath": "/dev/sda",
    "serialNumber": "SANITIZED-QS-A", "slot": 0, "healthStatus": "ONLINE",
}
POOL = {"id": "pool-a", "name": "SHARED-POOL", "storageSystemId": "node-a"}
# Node B reports the same physical disk, admitted through its stable ID, at
# its own local path.
PEER_ROW = {
    "id": "member-peer", "physicalDiskId": "disk-a", "physicalDiskSerialNumber": "SANITIZED-QS-A",
    "storageSystemId": "node-b", "storagePoolId": "pool-a", "devicePath": "/dev/sdq", "slot": 0,
    "physicalDiskObj": {"id": "disk-a", "devicePath": "/dev/sdq", "storageSystemId": "node-b"},
}
TWO_NODES = ({"id": "node-a"}, {"id": "node-b"})


def _raw(*, disks, pools=(), pool_devices=(), systems=TWO_NODES, hw_disks=()) -> TrueNASRawData:
    return TrueNASRawData(
        enclosures=[], disks=list(disks), pools=list(pools), pool_devices=list(pool_devices),
        systems=list(systems), disk_temperatures={}, smart_test_results=[], hw_disks=list(hw_disks),
    )


READABLE = {"smartHealthTest": "PASSED", "powerOnHours": 100}

# Source key -> published summary field, for every unsigned measurement.
UNSIGNED_FIELDS = {
    "logicalBlockSize": "logical_block_size",
    "physicalBlockSize": "physical_block_size",
    "rotationRate": "rotation_rate_rpm",
    "ssdLifeLeft": "endurance_remaining_percent",
    "mediumErrors": "media_errors",
    "predictiveErrors": "predictive_errors",
    "errCountNonMedium": "non_medium_errors",
    "errCountUncorrectedRead": "uncorrected_read_errors",
    "errCountUncorrectedWrite": "uncorrected_write_errors",
    "bytesRead": "bytes_read",
    "bytesWritten": "bytes_written",
}


class QuantastorNegativeUnsignedReadingTests(unittest.TestCase):
    """#1299."""

    def test_negative_unsigned_values_are_not_read(self) -> None:
        service = _service()
        for negative in (-1, -1.0, "-1", "-4,096"):
            with self.subTest(value=negative):
                summary = _summary(service, {**READABLE, **{key: negative for key in UNSIGNED_FIELDS}})
                for field_name in UNSIGNED_FIELDS.values():
                    self.assertIsNone(getattr(summary, field_name), field_name)
                    self.assertIn(field_name, summary.unread_fields)
                self.assertIsNone(summary.annualized_bytes_read)
                self.assertIsNone(summary.annualized_bytes_written)
                # The readable lifetime is untouched by the refusals.
                self.assertEqual(summary.power_on_hours, 100)
                self.assertNotIn("power_on_hours", summary.unread_fields)

    def test_each_negative_field_is_refused_on_its_own(self) -> None:
        service = _service()
        for key, field_name in UNSIGNED_FIELDS.items():
            with self.subTest(key=key):
                summary = _summary(service, {**READABLE, key: -1})
                self.assertIsNone(getattr(summary, field_name))
                self.assertEqual(summary.unread_fields, [field_name])

    def test_negative_block_size_does_not_fall_back_to_the_same_negative_slot_value(self) -> None:
        # The disk record reads the same raw field into the slot; a refused raw
        # value must not come back through that fallback.
        service = _service()
        record = service._build_quantastor_disk_records(
            _raw(disks=[{**DISK_A, "logicalBlockSize": -1, "physicalBlockSize": "-512"}]), "node-a",
        )[0]
        self.assertIsNone(record.logical_block_size)
        self.assertIsNone(record.physical_block_size)

    def test_refused_rotation_rate_on_an_ssd_stays_not_read(self) -> None:
        # Behaviour choice: a reported negative rate is refused, not replaced by
        # the solid-state default of zero.
        summary = _summary(_service(), {**READABLE, "rotationRate": -1, "isSsd": True})
        self.assertIsNone(summary.rotation_rate_rpm)
        self.assertIn("rotation_rate_rpm", summary.unread_fields)

    def test_zeroes_and_positive_values_stay_measurements(self) -> None:
        service = _service()
        zeros = {key: 0 for key in UNSIGNED_FIELDS if "BlockSize" not in key}
        summary = _summary(service, {**READABLE, **zeros, "logicalBlockSize": 512, "physicalBlockSize": "4096"})
        for key, field_name in UNSIGNED_FIELDS.items():
            if "BlockSize" in key:
                continue
            self.assertEqual(getattr(summary, field_name), 0, field_name)
        self.assertEqual(summary.logical_block_size, 512)
        self.assertEqual(summary.physical_block_size, 4096)
        self.assertEqual(summary.unread_fields, [])
        self.assertEqual(summary.annualized_bytes_read, 0)

    def test_negative_celsius_stays_a_reading(self) -> None:
        summary = _summary(_service(), {**READABLE, "temperature": -5})
        self.assertEqual(summary.temperature_c, -5)
        self.assertEqual(summary.unread_fields, [])

    def test_slot_fallback_still_supplies_a_refused_block_size(self) -> None:
        # Another source's real reading for the bay still wins over "not read".
        summary = _summary(_service(), {**READABLE, "logicalBlockSize": -1}, logical_block_size=512)
        self.assertEqual(summary.logical_block_size, 512)
        self.assertNotIn("logical_block_size", summary.unread_fields)


class QuantastorFractionalNegativeLifetimeTests(unittest.TestCase):
    """#1300."""

    def test_fractional_negative_power_on_is_not_a_zero_lifetime(self) -> None:
        service = _service()
        for key, value in itertools.product(("powerOnHours", "powerOnTimeHours", "powerOnTime"), (-0.5, "-0.5")):
            with self.subTest(key=key, value=value):
                summary = _summary(service, {"smartHealthTest": "PASSED", key: value})
                self.assertIsNone(summary.power_on_hours)
                self.assertIsNone(summary.power_on_days)
                self.assertEqual(summary.unread_fields, ["power_on_days", "power_on_hours"])

    def test_real_zero_and_positive_lifetimes_and_minus_one_are_unchanged(self) -> None:
        service = _service()
        for key in ("powerOnHours", "powerOnTimeHours", "powerOnTime"):
            with self.subTest(key=key):
                zero = _summary(service, {"smartHealthTest": "PASSED", key: 0})
                self.assertEqual((zero.power_on_hours, zero.power_on_days, zero.unread_fields), (0, 0, []))
                hours = _summary(service, {"smartHealthTest": "PASSED", key: "48"})
                self.assertEqual((hours.power_on_hours, hours.power_on_days, hours.unread_fields), (48, 2, []))
                refused = _summary(service, {"smartHealthTest": "PASSED", key: -1})
                self.assertIsNone(refused.power_on_hours)
                self.assertEqual(refused.unread_fields, ["power_on_days", "power_on_hours"])

    def test_fractional_negative_lifetime_alone_keeps_its_not_read_marker(self) -> None:
        summary = _summary(_service(), {"powerOnHours": "-0.5"})
        self.assertIsNone(summary.power_on_hours)
        self.assertEqual(summary.unread_fields, ["power_on_days", "power_on_hours"])


class QuantastorPeerSmartPathTests(unittest.TestCase):
    """#1318."""

    def test_peer_row_admitted_by_stable_identity_adds_no_smart_path(self) -> None:
        service = _service()
        own_row = {**PEER_ROW, "id": "member-own", "storageSystemId": "node-a", "devicePath": "/dev/sda",
                   "physicalDiskObj": {"id": "disk-a", "devicePath": "/dev/sda", "storageSystemId": "node-a"}}
        for rows in ([PEER_ROW], [PEER_ROW, own_row], [own_row, PEER_ROW]):
            with self.subTest(rows=[row["id"] for row in rows]):
                record = service._build_quantastor_disk_records(
                    _raw(disks=[DISK_A], pools=[POOL], pool_devices=rows), "node-a",
                )[0]
                # The peer row is still admitted for placement.
                self.assertEqual(record.slot, 0)
                self.assertIn("quantastor_pool_device", record.raw)
                self.assertEqual(record.smart_devices, ["sda"])

    def test_peer_row_in_either_disk_order_adds_no_smart_path(self) -> None:
        service = _service()
        disk_b = {**DISK_A, "id": "disk-b", "storageSystemId": "node-b", "devicePath": "/dev/sda",
                  "serialNumber": "SANITIZED-QS-B", "slot": 1}
        for order in (1, -1):
            with self.subTest(order=order):
                records = service._build_quantastor_disk_records(
                    _raw(disks=[DISK_A, disk_b][::order], pools=[POOL], pool_devices=[PEER_ROW]), "node-a",
                )
                self.assertEqual([(record.serial, record.smart_devices) for record in records],
                                 [("SANITIZED-QS-A", ["sda"])])

    def test_peer_hardware_row_adds_no_smart_path(self) -> None:
        # The same rule for the hardware-disk row admitted by serial.
        service = _service()
        disk = {key: value for key, value in DISK_A.items() if key != "devicePath"}
        disk["name"] = "disk-a"
        peer_hw = {"id": "hw-peer", "serialNum": "SANITIZED-QS-A", "storageSystemId": "node-b",
                   "enclosureId": "shelf-a", "slot": 0, "devicePath": "/dev/sdq"}
        record = service._build_quantastor_disk_records(
            _raw(disks=[disk], hw_disks=[peer_hw]), "node-a",
        )[0]
        self.assertNotIn("sdq", record.smart_devices)
        self.assertNotIn("/dev/sdq", record.smart_devices)

    def test_own_node_rows_still_contribute_their_paths(self) -> None:
        service = _service()
        disk = {key: value for key, value in DISK_A.items() if key != "devicePath"}
        own_row = {**PEER_ROW, "storageSystemId": "node-a", "devicePath": "/dev/disk/by-id/scsi-SANITIZED",
                   "physicalDiskObj": {"devicePath": "/dev/sdc"}}
        own_hw = {"id": "hw-own", "serialNum": "SANITIZED-QS-A", "storageSystemId": "node-a",
                  "enclosureId": "shelf-a", "slot": 0, "devicePath": "/dev/sdd"}
        record = service._build_quantastor_disk_records(
            _raw(disks=[disk], pools=[POOL], pool_devices=[own_row], hw_disks=[own_hw]), "node-a",
        )[0]
        self.assertEqual(record.smart_devices, ["disk/by-id/scsi-SANITIZED", "sdd", "sdc"])

    def test_single_node_rows_without_an_owner_still_contribute_their_paths(self) -> None:
        service = _service()
        disk = {key: value for key, value in DISK_A.items() if key not in ("devicePath", "storageSystemId")}
        row = {key: value for key, value in PEER_ROW.items() if key not in ("storageSystemId", "physicalDiskObj")}
        record = service._build_quantastor_disk_records(
            _raw(disks=[disk], pools=[POOL], pool_devices=[row], systems=({"id": "node-a"},)), "node-a",
        )[0]
        self.assertEqual(record.smart_devices, ["sdq"])


def _slot_from_record(record, **overrides) -> SlotView:
    fields = {
        "slot": 0, "slot_label": "00", "row_index": 0, "column_index": 0, "present": True,
        "serial": record.serial, "device_name": record.device_name, "enclosure_id": "shelf-a",
        "logical_block_size": record.logical_block_size, "physical_block_size": record.physical_block_size,
        "raw_status": {"disk_raw": record.raw}, "smart_device_names": list(record.smart_devices),
    }
    fields.update(overrides)
    return SlotView(**fields)


# Saved before the node of the SMART paths was recorded: entries written by the
# earlier writer carry no such key.
SMART_NODE_KEY = "quantastor_smart_device_node"


class QuantastorSavedSlotDetailTests(unittest.TestCase):
    """Saved slot details must not bring a refused value back (#1299, #1318).

    Each test saves an entry, reloads it through a new service and store, and
    reads the consumer after the backfill, as a restart after an upgrade does.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "slot_details.json")

    def _save(self, slot: SlotView, summary: SmartSummaryView | None, *, legacy: bool) -> dict:
        service = _service()
        service.slot_detail_store = SlotDetailStore(self.path)
        entry = service._build_slot_detail_entry(slot, smart_summary=summary, loaded_entries={})
        if entry is None:
            self.fail("the writer saved nothing")
        if legacy:
            # The earlier writer wrote the same fields and no node.
            entry.slot_fields.pop(SMART_NODE_KEY, None)
        service.slot_detail_store.save_entries([entry])
        return dict(entry.slot_fields)

    def _reload(self, raw: TrueNASRawData) -> tuple[InventoryService, SlotView]:
        service = _service()
        service.slot_detail_store = SlotDetailStore(self.path)
        slot = _slot_from_record(service._build_quantastor_disk_records(raw, "node-a")[0])
        service._apply_persisted_slot_details([slot])
        return service, slot

    # A1 (#1318)
    def test_saved_peer_path_from_an_earlier_writer_is_not_a_candidate_after_reload(self) -> None:
        disk = {key: value for key, value in {**DISK_A, **READABLE}.items() if key != "devicePath"}
        raw = _raw(disks=[disk], pools=[POOL], pool_devices=[PEER_ROW])
        record = _service()._build_quantastor_disk_records(raw, "node-a")[0]
        self.assertEqual(record.smart_devices, [])
        # The earlier builder lent node-b's path, and its writer saved it.
        saved = self._save(_slot_from_record(record, smart_device_names=["sdq"]), None, legacy=True)
        self.assertEqual(saved["smart_device_names"], ["sdq"])

        _service_after, slot = self._reload(raw)
        self.assertEqual(slot.smart_device_names, [])
        self.assertEqual(InventoryService._smart_candidate_devices(slot), [])

    def test_saved_path_recorded_for_another_node_is_not_a_candidate(self) -> None:
        disk = {key: value for key, value in {**DISK_A, **READABLE}.items() if key != "devicePath"}
        raw = _raw(disks=[disk], pools=[POOL], pool_devices=[PEER_ROW])
        record = _service()._build_quantastor_disk_records(raw, "node-a")[0]
        peer_view = {**record.raw, "storageSystemId": "node-b"}
        self._save(_slot_from_record(record, smart_device_names=["sdq"], raw_status={"disk_raw": peer_view}),
                   None, legacy=False)

        _service_after, slot = self._reload(raw)
        self.assertEqual(InventoryService._smart_candidate_devices(slot), [])

    def test_saved_same_node_path_is_still_a_candidate_after_reload(self) -> None:
        # Control: the current poll omits the disk's path; the path saved on
        # this node is still used.
        disk = {**DISK_A, **READABLE}
        record = _service()._build_quantastor_disk_records(_raw(disks=[disk]), "node-a")[0]
        self.assertEqual(record.smart_devices, ["sda"])
        saved = self._save(_slot_from_record(record), None, legacy=False)
        self.assertEqual(saved["smart_device_names"], ["sda"])

        current = {key: value for key, value in disk.items() if key != "devicePath"}
        _service_after, slot = self._reload(_raw(disks=[current]))
        self.assertEqual(slot.smart_device_names, ["sda"])
        self.assertEqual(InventoryService._smart_candidate_devices(slot), ["sda"])

    # A2 (#1299)
    def test_saved_negative_block_sizes_stay_not_read_after_reload(self) -> None:
        disk = {**DISK_A, **READABLE, "logicalBlockSize": -1, "physicalBlockSize": -512}
        raw = _raw(disks=[disk])
        record = _service()._build_quantastor_disk_records(raw, "node-a")[0]
        # The earlier record and summary published the negative sizes, and
        # its writer saved them in both halves of the entry.
        earlier = SmartSummaryView(available=True, logical_block_size=-1, physical_block_size=-512,
                                   rotation_rate_rpm=-1, power_on_hours=100, power_on_days=4)
        saved = self._save(_slot_from_record(record, logical_block_size=-1, physical_block_size=-512),
                           earlier, legacy=True)
        self.assertEqual((saved["logical_block_size"], saved["physical_block_size"]), (-1, -512))

        service, slot = self._reload(raw)
        self.assertIsNone(slot.logical_block_size)
        self.assertIsNone(slot.physical_block_size)
        summary = service._merge_smart_summary(slot, service._build_quantastor_smart_summary(slot))
        self.assertIsNone(summary.logical_block_size)
        self.assertIsNone(summary.physical_block_size)
        self.assertIn("logical_block_size", summary.unread_fields)
        self.assertIn("physical_block_size", summary.unread_fields)
        # The saved SMART half is not a reading of them either.
        persisted = service._build_persisted_smart_summary(slot)
        if persisted is None:
            self.fail("no saved SMART half was served")
        self.assertIsNone(persisted.logical_block_size)
        self.assertIsNone(persisted.physical_block_size)
        self.assertIsNone(persisted.rotation_rate_rpm)
        self.assertEqual(persisted.unread_fields, ["logical_block_size", "physical_block_size", "rotation_rate_rpm"])
        self.assertEqual(persisted.power_on_hours, 100)

    def test_saved_non_negative_block_sizes_are_still_shown_after_reload(self) -> None:
        # Control: a real cached reading fills a poll that omits it.
        disk = {**DISK_A, **READABLE, "logicalBlockSize": 512, "physicalBlockSize": 4096}
        record = _service()._build_quantastor_disk_records(_raw(disks=[disk]), "node-a")[0]
        self._save(_slot_from_record(record), SmartSummaryView(
            available=True, logical_block_size=512, physical_block_size=4096), legacy=True)

        current = {key: value for key, value in disk.items() if "BlockSize" not in key}
        service, slot = self._reload(_raw(disks=[current]))
        self.assertEqual((slot.logical_block_size, slot.physical_block_size), (512, 4096))
        summary = service._merge_smart_summary(slot, service._build_quantastor_smart_summary(slot))
        self.assertEqual((summary.logical_block_size, summary.physical_block_size), (512, 4096))
        self.assertEqual(summary.unread_fields, [])
        persisted = service._build_persisted_smart_summary(slot)
        if persisted is None:
            self.fail("no saved SMART half was served")
        self.assertEqual((persisted.logical_block_size, persisted.physical_block_size), (512, 4096))

    def test_merge_does_not_copy_a_negative_slot_block_size(self) -> None:
        # The final merge holds the same rule whatever filled the slot.
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, present=True,
                        logical_block_size=-1, physical_block_size=-512)
        summary = SmartSummaryView(available=True, power_on_hours=1,
                                   unread_fields=["logical_block_size", "physical_block_size"])
        merged = InventoryService._merge_smart_summary(slot, summary)
        self.assertIsNone(merged.logical_block_size)
        self.assertIsNone(merged.physical_block_size)
        self.assertEqual(merged.unread_fields, ["logical_block_size", "physical_block_size"])
        slot.logical_block_size, slot.physical_block_size = 0, 4096
        merged = InventoryService._merge_smart_summary(slot, SmartSummaryView(available=True))
        self.assertEqual((merged.logical_block_size, merged.physical_block_size), (0, 4096))


if __name__ == "__main__":
    unittest.main()
