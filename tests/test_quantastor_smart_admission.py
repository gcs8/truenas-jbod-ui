"""QuantaStor SMART admission: unsigned readings.

Synthetic fixtures only; no appliance payload or SMART command is used.

#1299: a negative value for an unsigned SMART measurement (block size, rotation
rate, endurance, error counters, byte totals) is not a reading. It stays
unknown and is named in ``unread_fields``; legitimate zeroes and negative
Celsius temperatures stay valid.

#1300: a negative power-on time between -1 and 0 (number or text) is refused
before conversion to whole hours, so it never becomes a measured zero.
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
from app.models.domain import SlotView
from app.services.inventory import InventoryService
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


if __name__ == "__main__":
    unittest.main()
