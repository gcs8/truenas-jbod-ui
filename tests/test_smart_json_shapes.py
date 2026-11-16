"""SMART and NVMe JSON summaries: unexpected shapes stay unknown (#1221).

A decoded payload that is not an object, a value of the wrong JSON type, a
boolean in a numeric field, or a non-finite or negative counter must never
raise through the typed summary boundary and must never become a reading.
Valid payloads are the controls: they still produce the same readings.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.config import Settings, SystemConfig
from app.models.domain import SlotView, SmartSummaryView
from app.services.inventory import InventoryService
from app.services.parsers import (
    parse_nvme_id_ctrl_summary,
    parse_nvme_id_ns_summary,
    parse_nvme_smart_log_summary,
    parse_smartctl_summary,
)
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore
from app.services.ssh_probe import SSHCommandResult

SMART_PARSE_FAILED = "SMART JSON parsing failed."
# Synthetic namespace identifiers in their documented widths (16 and 32 hex
# digits), built from parts so no literal identifier-shaped token is tracked.
EUI64_TEXT = "".join(("00a07510", "2b91c7cf"))
NGUID_TEXT = "".join(("00000000", "00000010", "00a07501", "2b91c7cf"))

# Valid JSON that is not an object, plus JSON that the decoder itself refuses
# with something other than JSONDecodeError (an over-long integer literal is a
# ValueError; deep nesting is a RecursionError).
NON_OBJECT_ROOTS = ("[]", "[{\"temperature\": {\"current\": 30}}]", "5", "\"text\"", "null", "true")
UNDECODABLE_ROOTS = ("{\"temperature\": {\"current\": " + "9" * 5000 + "}}", "[" * 100_000)


def _slot() -> SlotView:
    return SlotView(slot=0, slot_label="00", row_index=0, column_index=0, serial="SYNTHETIC-SMART-SHAPE")


def _summary(payload: object, slot: SlotView | None = None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return InventoryService._parse_smart_payload_for_slot(slot, text)


class SmartctlRootShapeTests(unittest.TestCase):
    def test_non_object_root_is_unavailable_not_an_exception(self) -> None:
        for root in NON_OBJECT_ROOTS + UNDECODABLE_ROOTS:
            with self.subTest(root=root[:40], reader="parse_smartctl_summary"):
                self.assertEqual(parse_smartctl_summary(root), {"available": False, "message": SMART_PARSE_FAILED})
            for slot in (None, _slot()):
                with self.subTest(root=root[:40], slot=slot is not None):
                    summary = _summary(root, slot)
                    self.assertFalse(summary.available)
                    # The SSH reader moves to the next candidate on this message.
                    self.assertEqual(summary.message, SMART_PARSE_FAILED)
                    self.assertIsNone(summary.temperature_c)

    def test_object_root_control_still_parses(self) -> None:
        summary = _summary({"serial_number": "SYNTHETIC-SMART-SHAPE", "temperature": {"current": 30}}, _slot())
        self.assertTrue(summary.available)
        self.assertEqual(summary.temperature_c, 30)


class SmartctlNumericAdmissionTests(unittest.TestCase):
    BOOLEAN_PAYLOAD = {
        "temperature": {"current": True},
        "power_on_time": {"hours": True},
        "logical_block_size": True,
        "physical_block_size": True,
        "rotation_rate": True,
        "nvme_smart_health_information_log": {
            "data_units_read": True,
            "data_units_written": True,
            "percentage_used": True,
            "available_spare": True,
            "available_spare_threshold": True,
            "media_errors": True,
            "unsafe_shutdowns": True,
        },
        "ata_smart_attributes": {"table": [
            {"id": 12, "raw": {"value": True}},
            {"id": 199, "raw": True},
        ]},
        "ata_device_statistics": {"pages": [{"table": [
            {"name": "Lifetime Power-On Resets", "value": True},
            {"name": "Number of Read Commands", "value": True},
        ]}]},
        "ata_smart_self_test_log": {"standard": {"table": [
            {"lifetime_hours": True},
        ]}},
        "nvme_namespaces": [{"eui64": {"oui": True, "ext_id": True}}],
    }
    NUMERIC_FIELDS = (
        "temperature_c", "power_on_hours", "power_on_days", "logical_block_size",
        "physical_block_size", "rotation_rate_rpm", "bytes_read", "bytes_written",
        "endurance_used_percent", "endurance_remaining_percent", "available_spare_percent",
        "available_spare_threshold_percent", "media_errors", "unsafe_shutdowns",
        "power_cycle_count", "interface_crc_errors", "power_on_resets", "read_commands",
        "last_test_lifetime_hours", "namespace_eui64",
    )

    def test_booleans_never_become_readings(self) -> None:
        for flag in (True, False):
            with self.subTest(flag=flag):
                payload = json.loads(json.dumps(self.BOOLEAN_PAYLOAD).replace("true", json.dumps(flag)))
                summary = _summary(payload)
                values = summary.model_dump()
                for field in self.NUMERIC_FIELDS:
                    self.assertIsNone(values[field], field)
                self.assertFalse(summary.available)

    def test_integer_controls_still_become_readings(self) -> None:
        payload = json.loads(json.dumps(self.BOOLEAN_PAYLOAD).replace("true", "2"))
        payload["nvme_namespaces"] = [{"eui64": {"oui": 1, "ext_id": 2}}]
        summary = _summary(payload)
        self.assertTrue(summary.available)
        self.assertEqual(summary.temperature_c, 2)
        self.assertEqual(summary.power_on_hours, 2)
        self.assertEqual(summary.logical_block_size, 2)
        self.assertEqual(summary.rotation_rate_rpm, 2)
        self.assertEqual(summary.bytes_written, 2 * 512_000)
        self.assertEqual(summary.endurance_used_percent, 2)
        self.assertEqual(summary.media_errors, 2)
        self.assertEqual(summary.power_cycle_count, 2)
        self.assertEqual(summary.interface_crc_errors, 2)
        self.assertEqual(summary.power_on_resets, 2)
        self.assertEqual(summary.last_test_lifetime_hours, 2)
        self.assertEqual(summary.namespace_eui64, f"eui.{1:06x}{2:010x}")

    def test_zero_integer_is_a_reading_not_unknown(self) -> None:
        summary = _summary({"temperature": {"current": 0}, "power_on_time": {"hours": 0}})
        self.assertTrue(summary.available)
        self.assertEqual((summary.temperature_c, summary.power_on_hours), (0, 0))

    def test_missing_fields_stay_unknown(self) -> None:
        summary = _summary({"temperature": {}, "power_on_time": {}})
        self.assertFalse(summary.available)
        self.assertIsNone(summary.temperature_c)
        self.assertIsNone(summary.power_on_hours)


class ScsiByteCounterTests(unittest.TestCase):
    @staticmethod
    def _payload(token: str) -> str:
        # ``token`` is raw JSON text, so the decoder's non-standard Infinity
        # and NaN literals can be exercised as well as quoted strings.
        return (
            '{"temperature": {"current": 30}, "scsi_error_counter_log": {'
            '"read": {"gigabytes_processed": ' + token + '}, '
            '"write": {"gigabytes_processed": ' + token + '}}}'
        )

    def test_invalid_counters_stay_unknown(self) -> None:
        tokens = (
            '"Infinity"', '"-Infinity"', '"inf"', '"NaN"', '"sNaN"', '"-1.5"', '"-0.001"', '"1e5000"',
            "Infinity", "-Infinity", "NaN", "1e400", "-2", "true", "false",
        )
        for token in tokens:
            with self.subTest(token=token):
                summary = _summary(self._payload(token))
                self.assertTrue(summary.available)
                self.assertEqual(summary.temperature_c, 30)
                self.assertIsNone(summary.bytes_read)
                self.assertIsNone(summary.bytes_written)

    def test_valid_counters_are_converted(self) -> None:
        for token, expected in (('"1.5"', 1_500_000_000), ("2", 2_000_000_000), ("0.25", 250_000_000),
                                ('"0"', 0), ("0", 0), ('" 3,000.5 "', None)):
            with self.subTest(token=token):
                summary = _summary(self._payload(token))
                self.assertEqual(summary.bytes_read, expected)
                self.assertEqual(summary.bytes_written, expected)


class ScsiByteCounterBoundaryTests(unittest.TestCase):
    """Decimal exponents past the arithmetic context are checked before use."""

    @staticmethod
    def _read(token: str):
        return _summary({"temperature": {"current": 30},
                         "scsi_error_counter_log": {"read": {"gigabytes_processed": token}}})

    def test_decimal_exponent_overflow_is_unknown(self) -> None:
        for token in ("1e1000000", "9e999999999", "1e999999999999999999"):
            with self.subTest(token=token):
                summary = self._read(token)
                self.assertTrue(summary.available)
                self.assertEqual(summary.temperature_c, 30)
                self.assertIsNone(summary.bytes_read)

    def test_negative_counter_underflow_is_unknown(self) -> None:
        # Multiplication rounds these to (negative) zero; the sign is checked first.
        for token in ("-1e-1000100", "-0", "-0.0", "-1e-30"):
            with self.subTest(token=token):
                summary = self._read(token)
                self.assertEqual(summary.temperature_c, 30)
                self.assertIsNone(summary.bytes_read)

    def test_counter_cap_boundary_and_controls(self) -> None:
        limit_gb = Decimal(2**64) / Decimal(10**9)
        for token, expected in (
            ("0", 0), ("1.5", 1_500_000_000),
            (str(limit_gb - Decimal("0.000000001")), 2**64 - 1),
            (str(limit_gb), None),
        ):
            with self.subTest(token=token):
                self.assertEqual(self._read(token).bytes_read, expected)


class SectorSizeAdmissionTests(unittest.TestCase):
    """A rejected logical block size yields no sector-derived byte total."""

    SECTOR_SOURCES = {
        "device statistics": {"ata_device_statistics": {"pages": [{"table": [
            {"name": "Logical Sectors Read", "value": 2},
            {"name": "Logical Sectors Written", "value": 3},
        ]}]}},
        "raw attributes": {"ata_smart_attributes": {"table": [
            {"id": 242, "name": "Total_LBAs_Read", "raw": {"value": 2}},
            {"id": 241, "name": "Total_LBAs_Written", "raw": {"value": 3}},
        ]}},
    }

    def _read(self, source: str, **extra):
        return _summary({"temperature": {"current": 30}, **self.SECTOR_SOURCES[source], **extra})

    def test_rejected_sector_size_does_not_invent_bytes(self) -> None:
        for source in self.SECTOR_SOURCES:
            for size in (True, False, [], {}, -512, 0, None, "4096", 4096.0):
                with self.subTest(source=source, size=size):
                    summary = self._read(source, logical_block_size=size)
                    self.assertTrue(summary.available)
                    self.assertEqual(summary.temperature_c, 30)
                    self.assertIsNone(summary.logical_block_size)
                    self.assertIsNone(summary.bytes_read)
                    self.assertIsNone(summary.bytes_written)

    def test_valid_and_absent_sector_size_controls(self) -> None:
        for source in self.SECTOR_SOURCES:
            with self.subTest(source=source, size=4096):
                summary = self._read(source, logical_block_size=4096)
                self.assertEqual((summary.bytes_read, summary.bytes_written), (8192, 12288))
            with self.subTest(source=source, size="absent"):
                # An absent field keeps the long-standing 512-byte default.
                summary = self._read(source)
                self.assertEqual((summary.bytes_read, summary.bytes_written), (1024, 1536))

    def test_independent_byte_counters_survive_rejected_size(self) -> None:
        summary = _summary({
            "temperature": {"current": 30}, "logical_block_size": True,
            "scsi_error_counter_log": {"read": {"gigabytes_processed": "1.5"}},
            "ata_smart_attributes": {"table": [
                {"id": 241, "name": "Host_Writes_32MiB", "raw": {"value": 2}},
            ]},
        })
        self.assertEqual(summary.bytes_read, 1_500_000_000)
        self.assertEqual(summary.bytes_written, 2 * 32 * 1024**2)


class OversizedIntegerTests(unittest.TestCase):
    """In-limit JSON integers too large for float arithmetic stay unknown."""

    HUGE = 10**400

    def test_oversized_kelvin_is_unknown(self) -> None:
        cases = (
            (parse_nvme_smart_log_summary, "temperature", "temperature_c"),
            (parse_nvme_id_ctrl_summary, "wctemp", "warning_temperature_c"),
            (parse_nvme_id_ctrl_summary, "cctemp", "critical_temperature_c"),
        )
        for reader, key, field in cases:
            for value in (self.HUGE, 65536):
                with self.subTest(field=field, value=str(value)[:8]):
                    result = reader(json.dumps({key: value, "fr": "FW01", "power_on_hours": 5}))
                    self.assertIsNone(result[field])
            with self.subTest(field=field, value="control"):
                self.assertEqual(reader(json.dumps({key: 348}))[field], 75)

    def test_oversized_byte_counters_are_unknown(self) -> None:
        huge = self.HUGE
        payloads = (
            {"nvme_smart_health_information_log": {"data_units_written": huge, "percentage_used": 3}},
            {"ata_smart_attributes": {"table": [{"id": 241, "raw": {"value": huge}}]}},
            {"ata_smart_attributes": {"table": [{"id": 241, "name": "Host_Writes_32MiB", "raw": {"value": huge}}]}},
            {"ata_device_statistics": {"pages": [{"table": [{"name": "Logical Sectors Written", "value": huge}]}]}},
            {"logical_block_size": huge,
             "ata_device_statistics": {"pages": [{"table": [{"name": "Logical Sectors Written", "value": 2}]}]}},
        )
        for extra in payloads:
            with self.subTest(source=next(iter(extra))):
                summary = _summary({"temperature": {"current": 30}, "power_on_time": {"hours": 1000}, **extra})
                self.assertEqual(summary.temperature_c, 30)
                self.assertIsNone(summary.bytes_written)
                self.assertIsNone(summary.annualized_bytes_written)
                self.assertIsNone(summary.estimated_lifetime_bytes_written)
        with self.subTest(source="nvme smart-log"):
            result = parse_nvme_smart_log_summary(json.dumps(
                {"temperature": 303, "data_units_written": huge, "power_on_hours": 1000, "percent_used": 3}))
            self.assertEqual(result["temperature_c"], 30)
            self.assertIsNone(result["bytes_written"])
            self.assertIsNone(result["annualized_bytes_written"])

    def test_large_valid_byte_counter_control(self) -> None:
        summary = _summary({"temperature": {"current": 30}, "power_on_time": {"hours": 8760},
                            "nvme_smart_health_information_log": {"data_units_written": 10**9, "percentage_used": 50}})
        self.assertEqual(summary.bytes_written, 10**9 * 512_000)
        self.assertEqual(summary.annualized_bytes_written, 10**9 * 512_000)
        self.assertEqual(summary.estimated_lifetime_bytes_written, 2 * 10**9 * 512_000)


class AtaAttributeIdentityTests(unittest.TestCase):
    def test_ata_unhashable_id_keeps_valid_sibling(self) -> None:
        for bad_id in ([], {}, [12], {"id": 12}, "12", 12.0, True):
            with self.subTest(bad_id=bad_id):
                summary = _summary({"temperature": {"current": 30}, "ata_smart_attributes": {"table": [
                    {"id": bad_id, "raw": {"value": 2}},
                    {"id": 12, "raw": {"value": 3}},
                    {"id": bad_id, "name": "Total_LBAs_Written", "raw": {"value": 9}},
                ]}})
                self.assertEqual(summary.temperature_c, 30)
                self.assertEqual(summary.power_cycle_count, 3)
                self.assertIsNone(summary.bytes_written)

    def test_ata_valid_id_control(self) -> None:
        summary = _summary({"temperature": {"current": 30}, "ata_smart_attributes": {"table": [
            {"id": 12, "raw": {"value": 3}},
            {"id": 241, "name": "Total_LBAs_Written", "raw": {"value": 2}},
        ]}})
        self.assertEqual((summary.power_cycle_count, summary.bytes_written), (3, 1024))


class SmartctlTextFieldShapeTests(unittest.TestCase):
    def test_non_string_text_fields_stay_unknown(self) -> None:
        cases = (
            ("firmware_version", lambda bad: bad, "firmware_version"),
            ("logical_unit_id", lambda bad: bad, "logical_unit_id"),
            ("form_factor", lambda bad: {"name": bad}, "form_factor"),
            ("nvme_version", lambda bad: {"string": bad}, "protocol_version"),
            ("device", lambda bad: {"protocol": bad}, "transport_protocol"),
            ("interface_speed", lambda bad: {"current": {"string": bad}}, "negotiated_link_rate"),
        )
        for key, build, field in cases:
            for bad in (5, True, 1.5, [], {}):
                with self.subTest(key=key, value=bad):
                    summary = _summary({"temperature": {"current": 30}, key: build(bad)})
                    self.assertTrue(summary.available)
                    self.assertEqual(summary.temperature_c, 30)
                    self.assertIsNone(getattr(summary, field))

    def test_non_string_nested_text_stays_unknown(self) -> None:
        payloads = (
            {"ata_device_statistics": {"pages": [{"table": [{"name": 5, "value": 1}]}]}},
            {"ata_smart_attributes": {"table": [{"id": 241, "name": 5, "raw": {"value": 1}}]}},
            {"ata_smart_self_test_log": {"standard": {"table": [{"type": 5, "status": {"string": 5}}]}}},
            {"scsi_self_test_0": {"code": {"string": 5}, "result": {"name": 5}}},
            {"scsi_sas_port_0": {"phy_0": {"sas_address": 5, "attached_sas_address": 5,
                                           "negotiated_logical_link_rate": 5}}},
        )
        for extra in payloads:
            with self.subTest(extra=next(iter(extra))):
                summary = _summary({"temperature": {"current": 30}, **extra})
                self.assertTrue(summary.available)
                self.assertEqual(summary.temperature_c, 30)
                self.assertIsNone(summary.last_test_type)
                self.assertIsNone(summary.last_test_status)
                self.assertIsNone(summary.sas_address)
                self.assertIsNone(summary.negotiated_link_rate)

    def test_text_controls_still_parse(self) -> None:
        summary = _summary({
            "firmware_version": " FW01 ", "logical_unit_id": "0x1234abcd",
            "form_factor": {"name": "3.5 inches"}, "device": {"protocol": "SCSI"},
            "scsi_self_test_0": {"code": {"string": "Background short"}, "result": {"string": "Completed"}},
        })
        self.assertEqual(summary.firmware_version, "FW01")
        self.assertEqual(summary.logical_unit_id, "0x1234abcd")
        self.assertEqual(summary.form_factor, "3.5 inches")
        self.assertEqual(summary.transport_protocol, "SCSI")
        self.assertEqual(summary.last_test_type, "Background short")
        self.assertEqual(summary.last_test_status, "Completed")


class NvmeReaderShapeTests(unittest.TestCase):
    READERS = (
        (parse_nvme_smart_log_summary, "NVMe smart-log JSON parsing failed."),
        (parse_nvme_id_ctrl_summary, "NVMe id-ctrl JSON parsing failed."),
        (parse_nvme_id_ns_summary, "NVMe id-ns JSON parsing failed."),
    )

    def test_each_reader_refuses_non_object_root(self) -> None:
        for reader, message in self.READERS:
            for root in NON_OBJECT_ROOTS + UNDECODABLE_ROOTS:
                with self.subTest(reader=reader.__name__, root=root[:40]):
                    result = reader(root)
                    self.assertFalse(result["available"])
                    self.assertEqual(result["message"], message)

    def test_each_reader_object_control_parses(self) -> None:
        self.assertEqual(parse_nvme_smart_log_summary('{"temperature": 303}')["temperature_c"], 30)
        self.assertEqual(parse_nvme_id_ctrl_summary('{"fr": "FW01", "ver": 66304}')["protocol_version"], "1.3")
        self.assertEqual(parse_nvme_id_ns_summary(json.dumps({"eui64": EUI64_TEXT}))["namespace_eui64"], f"eui.{EUI64_TEXT}")

    def test_booleans_never_become_nvme_readings(self) -> None:
        for flag in (True, False):
            with self.subTest(flag=flag):
                smart_log = parse_nvme_smart_log_summary(json.dumps({
                    "temperature": flag, "power_on_hours": flag, "avail_spare": flag,
                    "spare_thresh": flag, "percent_used": flag, "data_units_read": flag,
                    "data_units_written": flag, "media_errors": flag, "unsafe_shutdowns": flag,
                }))
                self.assertFalse(smart_log["available"])
                for field in ("power_on_hours", "bytes_written", "endurance_used_percent", "media_errors"):
                    self.assertIsNone(smart_log[field], field)
                id_ctrl = parse_nvme_id_ctrl_summary(json.dumps({"fr": flag, "ver": flag, "wctemp": flag, "cctemp": flag}))
                self.assertFalse(id_ctrl["available"])
                id_ns = parse_nvme_id_ns_summary(json.dumps({"eui64": flag, "nguid": flag}))
                self.assertFalse(id_ns["available"])
                self.assertIsNone(id_ns["namespace_eui64"])
                self.assertIsNone(id_ns["namespace_nguid"])

    def test_enrichment_reader_with_slot_refuses_undecodable_id_ctrl(self) -> None:
        service = object.__new__(InventoryService)
        command_parsers = (("id-ctrl", parse_nvme_id_ctrl_summary),)
        for stdout in NON_OBJECT_ROOTS + UNDECODABLE_ROOTS + ('{"sn": 5, "fr": "FW01"}',):
            with self.subTest(stdout=stdout[:40]):
                results = {"id-ctrl": SSHCommandResult(command="id-ctrl", ok=True, stdout=stdout)}
                summary = service._parse_linux_nvme_enrichment_results(command_parsers, results, slot_view=_slot())
                self.assertIsNotNone(summary)
                if stdout.startswith("{\"sn\""):
                    self.assertEqual(summary.firmware_version, "FW01")
                else:
                    self.assertFalse(summary.available)


ATA_STATISTIC_FIELDS = {
    "Lifetime Power-On Resets": "power_on_resets",
    "Number of Read Commands": "read_commands",
    "Number of Write Commands": "write_commands",
    "Number of Hardware Resets": "hardware_resets",
    "Number of Interface CRC Errors": "interface_crc_errors",
    "Percentage Used Endurance Indicator": "endurance_used_percent",
    "Logical Sectors Read": "bytes_read",
    "Logical Sectors Written": "bytes_written",
}
ATA_ATTRIBUTE_FIELDS = {
    12: "power_cycle_count",
    199: "interface_crc_errors",
    232: "available_spare_percent",
    169: "available_spare_percent",
    241: "bytes_written",
    242: "bytes_read",
}
# A sector-valued attribute or statistic becomes bytes with the 512-byte default.
SECTOR_FIELDS = {"bytes_read", "bytes_written"}


def _statistic(name: str, value: object) -> dict:
    return {"ata_device_statistics": {"pages": [{"table": [{"name": name, "value": value}]}]}}


def _attribute(attribute_id: int, raw: object, **row: object) -> dict:
    return {"ata_smart_attributes": {"table": [{"id": attribute_id, "raw": raw, **row}]}}


class AtaNumericAdmissionTests(unittest.TestCase):
    """ATA statistic and attribute values are read only in their documented form."""

    def _read(self, extra: dict):
        return _summary({"temperature": {"current": 30}, **extra})

    def test_statistic_value_needs_an_exact_count(self) -> None:
        for name, field in ATA_STATISTIC_FIELDS.items():
            for bad in ("bad0", "bad42", "42", 2.0, -1, True, [], {}, None):
                with self.subTest(statistic=name, value=bad):
                    summary = self._read(_statistic(name, bad))
                    self.assertEqual(summary.temperature_c, 30)
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)

    def test_statistic_value_controls(self) -> None:
        for name, field in ATA_STATISTIC_FIELDS.items():
            scale = 512 if field in SECTOR_FIELDS else 1
            for good in (0, 42):
                with self.subTest(statistic=name, value=good):
                    summary = self._read(_statistic(name, good))
                    self.assertEqual(getattr(summary, field), good * scale)
                    self.assertEqual(summary.unread_fields, [])
            with self.subTest(statistic=name, value="not valid"):
                # smartctl leaves out the value of a statistic the drive marks invalid.
                summary = self._read({"ata_device_statistics": {"pages": [{"table": [{"name": name}]}]}})
                self.assertIsNone(getattr(summary, field))
                self.assertEqual(summary.unread_fields, [])

    def test_attribute_raw_value_needs_its_documented_form(self) -> None:
        bad_raws = (
            {"value": "bad42"}, {"value": "bad0"}, {"value": "42"}, {"value": 2.0}, {"value": -1},
            {"value": True}, {"value": None}, {"value": [42]}, {"value": "bad", "string": "42"},
            {"string": "bad42"}, {"string": "4 2"}, {"string": "-4"}, {"string": "²"}, {"string": "9" * 5000},
            {}, "bad42", "-4", 2.0, True, [], None,
        )
        for attribute_id, field in ATA_ATTRIBUTE_FIELDS.items():
            for raw in bad_raws:
                with self.subTest(attribute=attribute_id, raw=str(raw)[:30]):
                    summary = self._read(_attribute(attribute_id, raw))
                    self.assertEqual(summary.temperature_c, 30)
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)
            with self.subTest(attribute=attribute_id, raw="missing"):
                summary = self._read({"ata_smart_attributes": {"table": [{"id": attribute_id}]}})
                self.assertIsNone(getattr(summary, field))
                self.assertIn(field, summary.unread_fields)

    def test_attribute_raw_value_controls(self) -> None:
        good_raws = (
            ({"value": 0}, 0), ({"value": 42}, 42), ({"value": 42, "string": "42"}, 42),
            ({"string": "42"}, 42), ({"string": "42 (Min/Max 20/45)"}, 42), (42, 42), ("42", 42),
        )
        for attribute_id, field in ATA_ATTRIBUTE_FIELDS.items():
            scale = 512 if field in SECTOR_FIELDS else 1
            for raw, expected in good_raws:
                with self.subTest(attribute=attribute_id, raw=raw):
                    summary = self._read(_attribute(attribute_id, raw))
                    self.assertEqual(getattr(summary, field), expected * scale)
                    self.assertEqual(summary.unread_fields, [])

    def test_malformed_attribute_row_leaves_attribute_fields_unread(self) -> None:
        # An attribute row whose id cannot be read may be any attribute, so
        # an attribute this payload does not otherwise report is not read,
        # while a valid sibling attribute is still published.
        summary = self._read({"ata_smart_attributes": {"table": [
            {"id": [], "raw": {"value": 2}},
            {"id": 12, "raw": {"value": 3}},
        ]}})
        self.assertEqual(summary.power_cycle_count, 3)
        self.assertIsNone(summary.interface_crc_errors)
        self.assertIn("interface_crc_errors", summary.unread_fields)
        self.assertNotIn("power_cycle_count", summary.unread_fields)


class SmartNumericTokenTests(unittest.TestCase):
    """Numbers inside names and keys are bounded and checked before conversion."""

    def test_oversized_unit_multiplier_in_attribute_name_is_unread(self) -> None:
        for attribute_id, field in ((241, "bytes_written"), (242, "bytes_read")):
            for name in ("Host_Writes_" + "9" * 5000 + "MiB", "Host_Writes_" + "9" * 7 + "MiB",
                         "Host_Writes_²MiB", "Host_Writes_٣٢MiB", "Host_Writes_0MiB", "Host_Writes_032MiB"):
                with self.subTest(attribute=attribute_id, name=name[:24]):
                    summary = _summary({"temperature": {"current": 30}, **_attribute(attribute_id, {"value": 2}, name=name)})
                    self.assertEqual(summary.temperature_c, 30)
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)

    def test_malformed_self_test_key_makes_the_latest_test_unread(self) -> None:
        valid = {"code": {"string": "Background short"}, "result": {"string": "Completed"}, "power_on_time": {"hours": 4}}
        for key in ("scsi_self_test_" + "9" * 5000, "scsi_self_test_²", "scsi_self_test_٣",
                    "scsi_self_test_-1", "scsi_self_test_01", "scsi_self_test_", "scsi_self_test_1x"):
            with self.subTest(key=key[:24]):
                summary = _summary({"temperature": {"current": 30}, "scsi_self_test_0": valid, key: valid})
                self.assertEqual(summary.temperature_c, 30)
                # The malformed entry cannot be ordered, so no entry is the latest.
                for field in ("last_test_type", "last_test_status", "last_test_lifetime_hours"):
                    self.assertIsNone(getattr(summary, field), field)
                    self.assertIn(field, summary.unread_fields)

    def test_self_test_order_control(self) -> None:
        older = {"code": {"string": "Background long"}, "result": {"string": "Completed"}, "power_on_time": {"hours": 2}}
        newer = {"code": {"string": "Background short"}, "result": {"string": "Completed"}, "power_on_time": {"hours": 4}}
        summary = _summary({"power_on_time": {"hours": 10}, "scsi_self_test_1": older, "scsi_self_test_0": newer})
        self.assertEqual((summary.last_test_type, summary.last_test_lifetime_hours, summary.last_test_age_hours),
                         ("Background short", 4, 6))
        self.assertEqual(summary.unread_fields, [])


class AttributeUnitNameTests(unittest.TestCase):
    """A reported attribute name that cannot be read gives no byte total."""

    def _read(self, attribute_id: int, **row: object):
        return _summary({"temperature": {"current": 30}, **_attribute(attribute_id, {"value": 2}, **row)})

    def test_malformed_name_is_not_read_as_sectors(self) -> None:
        for attribute_id, field in ((241, "bytes_written"), (242, "bytes_read")):
            for name in (True, False, [], {}, None, 5, "", "  ", "Host_Writes_32MB", "Host_Writes_32mib"):
                with self.subTest(attribute=attribute_id, name=name):
                    summary = self._read(attribute_id, name=name)
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)

    def test_name_controls(self) -> None:
        for attribute_id, field in ((241, "bytes_written"), (242, "bytes_read")):
            for row, expected in (
                ({}, 1024),  # no name reported: the long-standing sector reading
                ({"name": "Total_LBAs_Written"}, 1024),
                ({"name": "Host_Writes_32MiB"}, 2 * 32 * 1024**2),
                ({"name": "Lifetime_Writes_GiB"}, 2 * 1024**3),
                ({"name": "Host_Writes_1KiB"}, 2 * 1024),
            ):
                with self.subTest(attribute=attribute_id, row=row):
                    summary = self._read(attribute_id, **row)
                    self.assertEqual(getattr(summary, field), expected)
                    self.assertEqual(summary.unread_fields, [])


class ByteCounterCapTests(unittest.TestCase):
    """The 2**64-byte cap is exclusive for every byte-total producer, not only SCSI."""

    def test_exact_cap_is_not_read_and_one_unit_below_is(self) -> None:
        cases = (
            ("sector statistic", lambda count: _statistic("Logical Sectors Written", count), 512, 2**55),
            ("sector attribute", lambda count: _attribute(241, {"value": count}), 512, 2**55),
            ("unit attribute", lambda count: _attribute(241, {"value": count}, name="Host_Writes_32MiB"),
             32 * 1024**2, 2**39),
            ("4096-byte sectors", lambda count: {"logical_block_size": 4096,
                                                 **_statistic("Logical Sectors Written", count)}, 4096, 2**52),
        )
        for label, build, unit, count_at_cap in cases:
            self.assertEqual(count_at_cap * unit, 2**64)
            with self.subTest(source=label, at="cap"):
                summary = _summary({"temperature": {"current": 30}, **build(count_at_cap)})
                self.assertIsNone(summary.bytes_written)
                self.assertIn("bytes_written", summary.unread_fields)
            with self.subTest(source=label, at="below"):
                summary = _summary({"temperature": {"current": 30}, **build(count_at_cap - 1)})
                self.assertEqual(summary.bytes_written, (count_at_cap - 1) * unit)
                self.assertEqual(summary.unread_fields, [])


class NvmeRotationTests(unittest.TestCase):
    def test_rejected_rotation_rate_is_not_inferred_for_nvme(self) -> None:
        for bad in (True, False, None, "0", [], {}, 1.5, -1):
            with self.subTest(value=bad):
                summary = _summary({"device": {"protocol": "NVMe"}, "temperature": {"current": 30}, "rotation_rate": bad})
                self.assertIsNone(summary.rotation_rate_rpm)
                self.assertIn("rotation_rate_rpm", summary.unread_fields)

    def test_rotation_controls(self) -> None:
        for payload, expected in (
            ({"device": {"protocol": "NVMe"}}, 0),  # absent: the NVMe inference stays
            ({"device": {"protocol": "NVMe"}, "rotation_rate": 0}, 0),
            ({"device": {"protocol": "SCSI"}, "rotation_rate": 7200}, 7200),
            ({"device": {"protocol": "SCSI"}}, None),
        ):
            with self.subTest(payload=payload):
                summary = _summary(payload)
                self.assertEqual(summary.rotation_rate_rpm, expected)
                self.assertEqual(summary.unread_fields, [])


# One malformed source per field parse_smartctl_summary publishes. Each payload
# also carries a valid sibling reading so the summary stays available.
_SELF_TEST = {"code": {"string": "Background short"}, "result": {"string": "Completed"}}
SMARTCTL_UNREAD_CASES = {
    "temperature_c": (
        {"temperature": {"current": "30"}}, {"temperature": 30},
        {"scsi_environmental_reports": {"temperature_1": {"current": "30"}}},
        # A rejected primary reading is not replaced by the environmental one.
        {"temperature": {"current": True}, "scsi_environmental_reports": {"temperature_1": {"current": 30}}},
    ),
    "warning_temperature_c": (
        {"nvme_composite_temperature_threshold": {"warning": "70"}},
        {"nvme_composite_temperature_threshold": {"warning": True}, "temperature": {"op_limit_max": 70}},
        {"temperature": {"op_limit_max": 70.5}},
    ),
    "critical_temperature_c": (
        {"nvme_composite_temperature_threshold": {"critical": "80"}},
        {"temperature": {"drive_trip": "x"}}, {"nvme_composite_temperature_threshold": 80},
    ),
    "smart_health_status": ({"smart_status": {"passed": "yes"}}, {"smart_status": True}),
    "power_cycle_count": (_attribute(12, {"value": "x"}),),
    "power_on_resets": (_statistic("Lifetime Power-On Resets", "x"),),
    "power_on_hours": ({"power_on_time": {"hours": True}}, {"power_on_time": 5}),
    "power_on_days": ({"power_on_time": {"hours": "48"}},),
    "last_test_type": ({"scsi_self_test_0": {**_SELF_TEST, "code": {"string": 5}}},
                       {"ata_smart_self_test_log": {"standard": {"table": [{"type": 5, "status": "Completed"}]}}},
                       {"ata_smart_self_test_log": {"standard": {"table": "x"}}}),
    "last_test_status": ({"scsi_self_test_0": {**_SELF_TEST, "result": []}},),
    "last_test_lifetime_hours": ({"scsi_self_test_0": {**_SELF_TEST, "power_on_time": {"hours": "4"}}},
                                 {"ata_smart_self_test_log": {"standard": {"table": [{"lifetime_hours": -4}]}}}),
    "last_test_age_hours": ({"power_on_time": {"hours": "x"},
                             "scsi_self_test_0": {**_SELF_TEST, "power_on_time": {"hours": 4}}},),
    "logical_block_size": ({"logical_block_size": "4096"},),
    "physical_block_size": ({"physical_block_size": 0}, {"physical_block_size": -512}),
    "available_spare_percent": ({"nvme_smart_health_information_log": {"available_spare": "x"}},
                                _attribute(232, {"value": "x"})),
    "available_spare_threshold_percent": ({"nvme_smart_health_information_log": {"available_spare_threshold": -1}},),
    "endurance_used_percent": ({"nvme_smart_health_information_log": {"percentage_used": "3"}},),
    "endurance_remaining_percent": ({"nvme_smart_health_information_log": {"percentage_used": "3"}},),
    "bytes_read": ({"nvme_smart_health_information_log": {"data_units_read": "x"}},
                   {"scsi_error_counter_log": {"read": {"gigabytes_processed": "x"}}},
                   {"scsi_error_counter_log": {"read": 5}}, {"scsi_error_counter_log": 5},
                   {"logical_block_size": True, **_statistic("Logical Sectors Read", 2)}),
    "bytes_written": ({"nvme_smart_health_information_log": {"data_units_written": 1.5}},
                      # A rejected first source stops the chain: no lower source stands in.
                      {"nvme_smart_health_information_log": {"data_units_written": "x"},
                       **_statistic("Logical Sectors Written", 2)}),
    "annualized_bytes_read": ({"power_on_time": {"hours": 1000},
                               "nvme_smart_health_information_log": {"data_units_read": "x"}},),
    "annualized_bytes_written": ({"power_on_time": {"hours": "x"},
                                  "nvme_smart_health_information_log": {"data_units_written": 5}},),
    "estimated_lifetime_bytes_written": ({"nvme_smart_health_information_log": {
        "data_units_written": 5, "percentage_used": "x"}},),
    "estimated_remaining_bytes_written": ({"nvme_smart_health_information_log": {
        "data_units_written": "x", "percentage_used": 3}},),
    "read_commands": (_statistic("Number of Read Commands", "x"),),
    "write_commands": (_statistic("Number of Write Commands", "x"),),
    "hardware_resets": (_statistic("Number of Hardware Resets", "x"),),
    "interface_crc_errors": (_statistic("Number of Interface CRC Errors", "x"), _attribute(199, {"value": "x"}),
                             {"ata_device_statistics": {"pages": "x"}}, {"ata_smart_attributes": {"table": {}}},
                             # A statistic row whose name cannot be read may be this statistic.
                             {"ata_device_statistics": {"pages": [{"table": [{"name": 5, "value": 1}]}]}},
                             {"ata_device_statistics": {"pages": [{"table": "x"}]}}),
    "media_errors": ({"nvme_smart_health_information_log": {"media_errors": "x"}},),
    "unsafe_shutdowns": ({"nvme_smart_health_information_log": {"unsafe_shutdowns": "x"}},),
    "rotation_rate_rpm": ({"rotation_rate": "7200"},),
    "form_factor": ({"form_factor": {"name": 5}}, {"form_factor": "2.5 inches"}),
    "firmware_version": ({"firmware_version": 5},),
    "protocol_version": ({"nvme_version": {"string": 5}}, {"nvme_version": 5, "sata_version": {"string": "SATA 3.3"}}),
    "namespace_eui64": ({"nvme_namespaces": [{"eui64": {"oui": "x", "ext_id": 1}}]},
                        {"nvme_namespaces": [{"eui64": {"oui": 2**24, "ext_id": 1}}]},
                        {"nvme_namespaces": [{"eui64": {"oui": 1, "ext_id": 2**40}}]},
                        {"nvme_namespaces": [5]}, {"nvme_namespaces": {}}),
    "read_cache_enabled": ({"read_lookahead": {"enabled": "yes"}}, {"read_lookahead": True}),
    "writeback_cache_enabled": ({"write_cache": {"enabled": 1}},),
    "transport_protocol": ({"device": {"protocol": 5}}, {"scsi_transport_protocol": {"name": 5}}),
    "logical_unit_id": ({"logical_unit_id": 5}, {"logical_unit_id": "not hex"}),
    "sas_address": ({"scsi_sas_port_0": {"phy_0": {"sas_address": 5}}},),
    "attached_sas_address": ({"scsi_sas_port_0": {"phy_0": {"sas_address": "0x5000c500aa", "attached_sas_address": 5}}},),
    "negotiated_link_rate": ({"interface_speed": {"current": {"string": 5}}},
                             {"scsi_sas_port_0": {"phy_0": {"negotiated_logical_link_rate": []}}}),
}
# Published by parse_smartctl_summary but not a scalar reading from one key.
SMARTCTL_FIELDS_WITHOUT_UNREAD_CASE = {
    "available", "message", "unread_fields",
    "namespace_nguid",  # always None in smartctl JSON; id-ns reads it
    "sas_ports", "sata_phy_events",  # collections with their own per-row admission
}


class UnreadFieldTests(unittest.TestCase):
    """A rejected SMART field is "not read": neither absent nor zero."""

    def test_every_published_field_has_an_unread_case(self) -> None:
        published = set(parse_smartctl_summary(json.dumps({"temperature": {"current": 30}})))
        self.assertEqual(published - SMARTCTL_FIELDS_WITHOUT_UNREAD_CASE, set(SMARTCTL_UNREAD_CASES))

    def test_rejected_source_is_listed_as_unread(self) -> None:
        for field, payloads in SMARTCTL_UNREAD_CASES.items():
            for extra in payloads:
                with self.subTest(field=field, payload=json.dumps(extra)[:60]):
                    sibling = {"firmware_version": "FW01"} if field != "firmware_version" else {"rotation_rate": 0}
                    summary = _summary({**sibling, **extra})
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)
                    self.assertEqual(summary.unread_fields, sorted(set(summary.unread_fields)))

    def test_absent_and_valid_sources_are_not_unread(self) -> None:
        for payload in ({}, {"temperature": {"current": 30}}, {"temperature": {}, "power_on_time": {}},
                        {"power_on_time": {"hours": 0}, "temperature": {"current": 0}}):
            with self.subTest(payload=payload):
                self.assertEqual(_summary(payload).unread_fields, [])

    def test_unread_only_payload_is_not_reported_as_no_fields(self) -> None:
        empty = _summary({})
        unread = _summary({"power_on_time": {"hours": True}})
        self.assertFalse(unread.available)
        self.assertEqual(unread.unread_fields, ["power_on_days", "power_on_hours"])
        self.assertEqual(empty.unread_fields, [])
        self.assertEqual(empty.message, "No SMART summary fields were returned for this disk.")
        self.assertEqual(unread.message, "SMART summary fields were returned for this disk but could not be read.")

    def test_nvme_readers_list_unread_fields(self) -> None:
        cases = (
            (parse_nvme_smart_log_summary, {"temperature": "303"}, ("temperature_c",)),
            (parse_nvme_smart_log_summary, {"power_on_hours": True}, ("power_on_days", "power_on_hours")),
            (parse_nvme_smart_log_summary, {"avail_spare": "x"}, ("available_spare_percent",)),
            (parse_nvme_smart_log_summary, {"spare_thresh": "x"}, ("available_spare_threshold_percent",)),
            (parse_nvme_smart_log_summary, {"percent_used": "x"},
             ("endurance_remaining_percent", "endurance_used_percent")),
            (parse_nvme_smart_log_summary, {"data_units_read": "x", "power_on_hours": 1000},
             ("annualized_bytes_read", "bytes_read")),
            (parse_nvme_smart_log_summary, {"media_errors": "x"}, ("media_errors",)),
            (parse_nvme_smart_log_summary, {"unsafe_shutdowns": "x"}, ("unsafe_shutdowns",)),
            (parse_nvme_id_ctrl_summary, {"fr": 5}, ("firmware_version",)),
            (parse_nvme_id_ctrl_summary, {"ver": "1.3"}, ("protocol_version",)),
            (parse_nvme_id_ctrl_summary, {"wctemp": 70000}, ("warning_temperature_c",)),
            (parse_nvme_id_ctrl_summary, {"cctemp": True}, ("critical_temperature_c",)),
            (parse_nvme_id_ns_summary, {"eui64": []}, ("namespace_eui64",)),
            (parse_nvme_id_ns_summary, {"nguid": {}}, ("namespace_nguid",)),
        )
        for reader, payload, fields in cases:
            with self.subTest(reader=reader.__name__, payload=payload):
                result = reader(json.dumps(payload))
                self.assertEqual(result["unread_fields"], sorted(fields))
        for reader in (parse_nvme_smart_log_summary, parse_nvme_id_ctrl_summary, parse_nvme_id_ns_summary):
            with self.subTest(reader=reader.__name__, payload="absent"):
                self.assertEqual(reader("{}")["unread_fields"], [])
        # 0 K is the documented "no threshold reported" value, not a rejected one.
        self.assertEqual(parse_nvme_id_ctrl_summary('{"wctemp": 0}')["unread_fields"], [])

    def test_merges_keep_unread_until_another_source_reads_the_field(self) -> None:
        unread = _summary({"temperature": {"current": True}, "power_on_time": {"hours": True}, "firmware_version": "FW01"})
        self.assertEqual(unread.unread_fields, ["power_on_days", "power_on_hours", "temperature_c"])
        supplement = _summary({"power_on_time": {"hours": 48}})
        merged = InventoryService._merge_missing_smart_fields(unread.model_copy(deep=True), supplement)
        self.assertEqual((merged.power_on_hours, merged.power_on_days), (48, 2))
        self.assertEqual(merged.unread_fields, ["temperature_c"])
        # Unread evidence from a supplement survives a primary that did not read the field.
        merged = InventoryService._merge_missing_smart_fields(_summary({"firmware_version": "FW01"}), unread)
        self.assertEqual(merged.unread_fields, ["power_on_days", "power_on_hours", "temperature_c"])
        # The inventory's own temperature fills the field, so it is no longer unread.
        slot = _slot()
        slot.temperature_c = 31
        merged = InventoryService._merge_smart_summary(slot, unread.model_copy(deep=True))
        self.assertEqual(merged.temperature_c, 31)
        self.assertEqual(merged.unread_fields, ["power_on_days", "power_on_hours"])

    def test_unread_list_is_not_a_reading_and_is_not_persisted(self) -> None:
        from app.services import inventory as inventory_module

        self.assertNotIn("unread_fields", inventory_module.SMART_SUMMARY_VALUE_FIELDS)
        self.assertNotIn("unread_fields", inventory_module.STABLE_SMART_DETAIL_FIELDS)
        merged = InventoryService._merge_smart_summary(_slot(), _summary({"power_on_time": {"hours": True}}))
        self.assertFalse(merged.available)
        self.assertEqual(merged.unread_fields, ["power_on_days", "power_on_hours"])
        self.assertEqual(SmartSummaryView().unread_fields, [])


class CelsiusRangeTests(unittest.TestCase):
    """A Celsius reading is an integer from -100 to 1000; anything else is not read."""

    def test_celsius_outside_the_supported_range_is_unread(self) -> None:
        for value in (10**400, 2**63, 1001, -101):
            for payload in ({"temperature": {"current": value}},
                            {"scsi_environmental_reports": {"temperature_1": {"current": value}}}):
                with self.subTest(value=str(value)[:12], source=next(iter(payload))):
                    summary = _summary({"smart_status": {"passed": True}, **payload})
                    self.assertEqual(summary.smart_health_status, "PASSED")
                    self.assertIsNone(summary.temperature_c)
                    self.assertIn("temperature_c", summary.unread_fields)
        # NVMe Kelvin words convert to Celsius outside the range: 1 K and 65535 K.
        for kelvin in (1, 65535):
            with self.subTest(kelvin=kelvin):
                self.assertEqual(parse_nvme_smart_log_summary(json.dumps({"temperature": kelvin}))["unread_fields"],
                                 ["temperature_c"])
                id_ctrl = parse_nvme_id_ctrl_summary(json.dumps({"wctemp": kelvin, "cctemp": kelvin}))
                self.assertEqual(id_ctrl["unread_fields"], ["critical_temperature_c", "warning_temperature_c"])

    def test_celsius_range_controls(self) -> None:
        for value in (-100, -1, 0, 30, 1000):
            with self.subTest(value=value):
                summary = _summary({"temperature": {"current": value}})
                self.assertEqual(summary.temperature_c, value)
                self.assertEqual(summary.unread_fields, [])
        for kelvin, celsius in ((173, -100), (303, 30), (348, 75)):
            with self.subTest(kelvin=kelvin):
                result = parse_nvme_smart_log_summary(json.dumps({"temperature": kelvin}))
                self.assertEqual((result["temperature_c"], result["unread_fields"]), (celsius, []))


class DocumentedFormTests(unittest.TestCase):
    """Raw strings, namespace identifiers and the version word keep their documented form."""

    def test_raw_string_member_must_be_text(self) -> None:
        for attribute_id, field in ATA_ATTRIBUTE_FIELDS.items():
            for raw in ({"string": 42}, {"string": 0}, {"string": True}, {"string": [42]}, {"string": None}):
                with self.subTest(attribute=attribute_id, raw=raw):
                    summary = _summary({"temperature": {"current": 30}, **_attribute(attribute_id, raw)})
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)

    def test_raw_compatibility_forms_still_read(self) -> None:
        # The bare integer and bare string forms are the older compatibility shape.
        for raw in ({"string": "42"}, {"value": 42, "string": "42"}, 42, "42"):
            with self.subTest(raw=raw):
                summary = _summary(_attribute(199, raw))
                self.assertEqual((summary.interface_crc_errors, summary.unread_fields), (42, []))

    def test_namespace_identifiers_need_their_hex_form_and_width(self) -> None:
        for field, key, bad_values in (
            ("namespace_eui64", "eui64", (-1, 1, 10**30, "zz-not-hex-00000", EUI64_TEXT + "aa",
                                          EUI64_TEXT[:14], "0x" + EUI64_TEXT, "0011 2233 4455 66")),
            ("namespace_nguid", "nguid", (-1, 10**40, "hello", "0" * 31, "0" * 33, "g" * 32)),
        ):
            for bad in bad_values:
                with self.subTest(field=field, value=str(bad)[:20]):
                    result = parse_nvme_id_ns_summary(json.dumps({key: bad}))
                    self.assertIsNone(result[field])
                    self.assertEqual(result["unread_fields"], [field])

    def test_namespace_identifier_controls(self) -> None:
        for payload, eui64, nguid in (
            ({"eui64": EUI64_TEXT}, f"eui.{EUI64_TEXT}", None),
            ({"eui64": EUI64_TEXT.upper()}, f"eui.{EUI64_TEXT}", None),
            ({"eui64": f"eui.{EUI64_TEXT}"}, f"eui.{EUI64_TEXT}", None),
            ({"nguid": NGUID_TEXT.upper()}, None, NGUID_TEXT),
        ):
            with self.subTest(payload=payload):
                result = parse_nvme_id_ns_summary(json.dumps(payload))
                self.assertEqual((result["namespace_eui64"], result["namespace_nguid"], result["unread_fields"]),
                                 (eui64, nguid, []))

    def test_version_word_must_fit_its_32_bits(self) -> None:
        for bad in (2**32, 2**40, 10**400):
            with self.subTest(value=str(bad)[:12]):
                result = parse_nvme_id_ctrl_summary(json.dumps({"ver": bad}))
                self.assertEqual((result["protocol_version"], result["unread_fields"]), (None, ["protocol_version"]))
        for good, expected in ((66304, "1.3"), (0x00020001, "2.0.1"), (2**32 - 1, "65535.255.255")):
            with self.subTest(value=good):
                result = parse_nvme_id_ctrl_summary(json.dumps({"ver": good}))
                self.assertEqual((result["protocol_version"], result["unread_fields"]), (expected, []))


class RefusedEvidenceTests(unittest.TestCase):
    """Refused evidence is listed as unread, never dropped and never replaced by a fallback."""

    def _read(self, extra: dict):
        return _summary({"firmware_version": "FW01", **extra})

    def test_rejected_temperature_limit_is_unread(self) -> None:
        for bad in (10**400, 1000, 151, 19, 10, -1):
            for key, field in (("op_limit_max", "warning_temperature_c"), ("limit_max", "critical_temperature_c"),
                               ("critical_limit_max", "critical_temperature_c"), ("drive_trip", "critical_temperature_c")):
                with self.subTest(key=key, value=str(bad)[:12]):
                    summary = self._read({"temperature": {"current": 30, key: bad}})
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)
                    self.assertEqual(summary.temperature_c, 30)

    def test_refused_nvme_threshold_is_not_replaced_by_the_fallback(self) -> None:
        for bad in (-1, 10**400, 1000):
            with self.subTest(value=str(bad)[:12]):
                summary = self._read({"nvme_composite_temperature_threshold": {"warning": bad, "critical": bad},
                                      "temperature": {"current": 30, "op_limit_max": 70, "limit_max": 80}})
                self.assertEqual((summary.warning_temperature_c, summary.critical_temperature_c), (None, None))
                self.assertIn("warning_temperature_c", summary.unread_fields)
                self.assertIn("critical_temperature_c", summary.unread_fields)

    def test_temperature_limit_controls(self) -> None:
        # Documented "not available" values stay absent, and the next source is still read.
        for sentinel in (0, 255, -128):
            with self.subTest(sentinel=sentinel):
                summary = self._read({"nvme_composite_temperature_threshold": {"warning": sentinel},
                                      "temperature": {"op_limit_max": 70, "drive_trip": sentinel}})
                self.assertEqual((summary.warning_temperature_c, summary.critical_temperature_c), (70, None))
                self.assertEqual(summary.unread_fields, [])
        for value in (20, 70, 150):
            with self.subTest(value=value):
                summary = self._read({"temperature": {"op_limit_max": value, "limit_max": value}})
                self.assertEqual((summary.warning_temperature_c, summary.critical_temperature_c), (value, value))
                self.assertEqual(summary.unread_fields, [])

    def test_invalid_attribute_id_leaves_attribute_fields_unread(self) -> None:
        for bad_id in (-1, 0, 256, 10**400):
            with self.subTest(attribute_id=str(bad_id)[:12]):
                summary = self._read({"ata_smart_attributes": {"table": [{"id": bad_id, "raw": {"value": 3}}]}})
                for field in ATA_ATTRIBUTE_FIELDS.values():
                    self.assertIsNone(getattr(summary, field), field)
                    self.assertIn(field, summary.unread_fields)

    def test_blank_statistic_name_leaves_statistic_fields_unread(self) -> None:
        for name in (" ", "", "\t"):
            with self.subTest(name=repr(name)):
                summary = self._read({"ata_device_statistics": {"pages": [{"table": [{"name": name, "value": 42}]}]}})
                for field in ATA_STATISTIC_FIELDS.values():
                    self.assertIsNone(getattr(summary, field), field)
                    self.assertIn(field, summary.unread_fields)

    def test_valid_uninteresting_rows_stay_absent(self) -> None:
        summary = self._read({
            "ata_smart_attributes": {"table": [{"id": 1, "raw": {"value": 0}}, {"id": 255, "raw": {"value": 0}}]},
            "ata_device_statistics": {"pages": [{"table": [{"name": "Some Other Statistic", "value": 1}]}]},
        })
        self.assertEqual(summary.unread_fields, [])

    def test_malformed_sas_port_or_phy_leaves_the_sas_projection_unread(self) -> None:
        valid_phy = {"sas_address": "0x5000c500aa", "attached_sas_address": "0x5000c500bb",
                     "negotiated_logical_link_rate": "phy enabled; 12 Gbps"}
        for label, ports in (
            ("phy not an object", {"scsi_sas_port_0": {"phy_0": True}}),
            ("port not an object", {"scsi_sas_port_0": [1]}),
            ("port scalar", {"scsi_sas_port_0": True}),
            ("malformed sibling port", {"scsi_sas_port_0": {"phy_0": valid_phy}, "scsi_sas_port_1": "x"}),
            ("malformed sibling phy", {"scsi_sas_port_0": {"phy_0": valid_phy, "phy_1": []}}),
        ):
            with self.subTest(case=label):
                # The interface speed is not a stand-in for a SAS link rate that was refused.
                summary = self._read({"interface_speed": {"current": {"string": "6.0 Gb/s"}}, **ports})
                for field in ("sas_address", "attached_sas_address", "negotiated_link_rate"):
                    self.assertIsNone(getattr(summary, field), field)
                    self.assertIn(field, summary.unread_fields)

    def test_sas_projection_controls(self) -> None:
        summary = self._read({"scsi_sas_port_0": {"num_phys": 1, "phy_0": {
            "sas_address": "0x5000c500aa", "attached_sas_address": "0x5000c500bb",
            "negotiated_logical_link_rate": "phy enabled; 12 Gbps"}}})
        self.assertEqual((summary.sas_address, summary.attached_sas_address, summary.negotiated_link_rate),
                         ("0x5000c500aa", "0x5000c500bb", "phy enabled; 12 Gbps"))
        self.assertEqual(summary.unread_fields, [])
        summary = self._read({"interface_speed": {"current": {"string": "6.0 Gb/s"}}})
        self.assertEqual((summary.negotiated_link_rate, summary.unread_fields), ("6.0 Gb/s", []))

    def test_self_test_object_without_its_value_is_unread(self) -> None:
        valid = {"type": {"string": "Short offline"}, "status": {"string": "Completed without error"}, "lifetime_hours": 10}
        for key, field in (("type", "last_test_type"), ("status", "last_test_status"),
                           ("lifetime_hours", "last_test_lifetime_hours")):
            for bad in ({"wrong": [42]}, {}):
                with self.subTest(key=key, value=bad):
                    row = {**valid, key: bad}
                    summary = self._read({"ata_smart_self_test_log": {"standard": {"table": [row]}}})
                    self.assertIsNone(getattr(summary, field))
                    self.assertIn(field, summary.unread_fields)
        for key, field in (("code", "last_test_type"), ("result", "last_test_status")):
            with self.subTest(key=key, log="scsi"):
                summary = self._read({"scsi_self_test_0": {**_SELF_TEST, key: {}}})
                self.assertIsNone(getattr(summary, field))
                self.assertIn(field, summary.unread_fields)

    def test_self_test_shape_controls(self) -> None:
        for row in (
            {"type": {"string": "Short offline"}, "status": {"string": "Completed without error"}, "lifetime_hours": 10},
            {"type": {"name": "Short offline"}, "status": {"name": "Completed without error"}, "lifetime_hours": {"hours": 10}},
            {"type": "Short offline", "status": "Completed without error", "lifetime_hours": 10},
        ):
            with self.subTest(row=row):
                summary = self._read({"ata_smart_self_test_log": {"standard": {"table": [row]}}})
                self.assertEqual((summary.last_test_type, summary.last_test_status, summary.last_test_lifetime_hours),
                                 ("Short offline", "Completed without error", 10))
                self.assertEqual(summary.unread_fields, [])

    def test_refused_transport_name_is_not_replaced_by_the_device_protocol(self) -> None:
        summary = self._read({"scsi_transport_protocol": {"name": 5}, "device": {"protocol": "SCSI"}})
        self.assertIsNone(summary.transport_protocol)
        self.assertIn("transport_protocol", summary.unread_fields)
        summary = self._read({"device": {"protocol": "NVMe"}})
        self.assertEqual((summary.transport_protocol, summary.unread_fields), ("NVMe", []))


class NegativePowerOnTimeTests(unittest.TestCase):
    """A negative power-on time is not a reading, from QuantaStor or from a stored summary.

    The summary carries no power-on hours or days and names the field not read,
    so the page says it was reported but not read. Zero and real values stay.
    """

    UNREAD = ["power_on_days", "power_on_hours"]

    def _quantastor(self, **disk: object) -> SmartSummaryView:
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, raw_status={"disk_raw": disk})
        return InventoryService._build_quantastor_smart_summary(object.__new__(InventoryService), slot)

    def _rebuilt(self, smart_fields: dict) -> SmartSummaryView | None:
        with tempfile.TemporaryDirectory() as directory:
            service = object.__new__(InventoryService)
            service.system = SystemConfig(id="synthetic")
            service.slot_detail_store = SlotDetailStore(str(Path(directory) / "slot-details.json"))
            service.slot_detail_store.save_entries([SlotDetailCacheEntry(
                system_id="synthetic", enclosure_id="enc-1", slot=0, identifiers=["SYNTHETIC-SERIAL"],
                slot_fields={"serial": "SYNTHETIC-SERIAL"}, smart_fields=smart_fields,
            )])
            slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, enclosure_id="enc-1",
                            present=True, serial="SYNTHETIC-SERIAL")
            return service._build_persisted_smart_summary(slot)

    def test_quantastor_negative_power_on_hours_is_not_read(self) -> None:
        for value in (-1, "-1", -24.0, "-48 hours"):
            with self.subTest(value=value):
                summary = self._quantastor(smartHealthTest="PASSED", powerOnHours=value, bytesWritten=8760)
                self.assertEqual((summary.power_on_hours, summary.power_on_days), (None, None))
                self.assertEqual(summary.unread_fields, self.UNREAD)
                self.assertIsNone(summary.annualized_bytes_written)
                self.assertEqual((summary.available, summary.smart_health_status), (True, "PASSED"))

    def test_quantastor_negative_power_on_alone_still_names_the_field(self) -> None:
        summary = self._quantastor(powerOnHours=-1)
        self.assertEqual((summary.power_on_hours, summary.power_on_days), (None, None))
        self.assertEqual(summary.unread_fields, self.UNREAD)

    def test_quantastor_zero_and_real_power_on_hours_are_readings(self) -> None:
        for value, days in ((0, 0), (240, 10), ("48", 2)):
            with self.subTest(value=value):
                summary = self._quantastor(smartHealthTest="PASSED", powerOnHours=value)
                self.assertEqual((summary.power_on_hours, summary.power_on_days), (int(value), days))
                self.assertEqual(summary.unread_fields, [])

    def test_quantastor_negative_temperature_is_still_a_reading(self) -> None:
        summary = self._quantastor(smartHealthTest="PASSED", temperature=-5, powerOnHours=10)
        self.assertEqual((summary.temperature_c, summary.power_on_hours, summary.unread_fields), (-5, 10, []))

    def test_stored_negative_power_on_comes_back_not_read(self) -> None:
        for fields in ({"power_on_hours": -1, "power_on_days": -1}, {"power_on_hours": -1}):
            with self.subTest(fields=fields):
                summary = self._rebuilt({"available": True, **fields})
                self.assertEqual((summary.power_on_hours, summary.power_on_days), (None, None))
                self.assertTrue(set(summary.unread_fields) & set(self.UNREAD))
                self.assertTrue(set(summary.unread_fields) <= set(self.UNREAD))

    def test_stored_negative_days_beside_real_hours_keeps_the_hours(self) -> None:
        summary = self._rebuilt({"available": True, "power_on_hours": 48, "power_on_days": -1})
        self.assertEqual((summary.power_on_hours, summary.power_on_days, summary.unread_fields), (48, 2, []))

    def test_stored_zero_and_real_power_on_are_readings(self) -> None:
        for hours, days in ((0, 0), (321, 13)):
            with self.subTest(hours=hours):
                summary = self._rebuilt({"available": True, "power_on_hours": hours, "power_on_days": days})
                self.assertEqual((summary.power_on_hours, summary.power_on_days, summary.unread_fields),
                                 (hours, days, []))

    def test_a_later_real_reading_clears_the_marker(self) -> None:
        refused = self._quantastor(smartHealthTest="PASSED", powerOnHours=-1)
        read = _summary({"power_on_time": {"hours": 100}})
        merged = InventoryService._merge_missing_smart_fields(refused, read)
        self.assertEqual((merged.power_on_hours, merged.power_on_days, merged.unread_fields), (100, 4, []))

    def test_fallback_for_a_refused_power_on_time_says_it_was_not_read(self) -> None:
        summary = self._quantastor(powerOnHours=-1)
        self.assertFalse(summary.available)
        self.assertEqual(
            summary.message,
            "QuantaStor reports this disk but no SMART counters. Its power-on time was reported but not read.",
        )

    def test_fallback_without_a_refusal_keeps_its_message(self) -> None:
        for disk in ({}, {"smartHealthTest": None}):
            with self.subTest(disk=disk):
                summary = self._quantastor(**disk)
                self.assertEqual(
                    (summary.message, summary.unread_fields),
                    ("QuantaStor reports this disk but no SMART counters.", []),
                )


class RefusedPowerOnStoreTests(unittest.TestCase):
    """A refused power-on time survives the slot-detail store and every stored reader.

    The refusal is written down as the lifetime names in ``unread_fields``; it
    replaces the last-good lifetime the way a reading does, and a later real
    reading clears it. Every reader that rebuilds a summary from the store,
    including the one used while the layout cannot be resolved, refuses a
    stored negative power-on time.
    """

    UNREAD = ["power_on_days", "power_on_hours"]
    SERIAL = "SYNTHETIC-SERIAL"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "slot-details.json"

    def _slot(self, **disk: object) -> SlotView:
        return SlotView(slot=0, slot_label="00", row_index=0, column_index=0, enclosure_id="enc-1",
                        present=True, serial=self.SERIAL, raw_status={"disk_raw": disk})

    def _service(self) -> InventoryService:
        # A fresh store object on the same file each time: nothing is shared in memory.
        service = object.__new__(InventoryService)
        service.system = SystemConfig(id="synthetic")
        service.settings = Settings()
        service.slot_detail_store = SlotDetailStore(str(self.path))
        service._smart_cache = {}
        service._smart_cache_until = {}
        service._smart_negative_cache = {}
        service._identity_unknown_slots = set()
        service._observe_smart_summary_request = lambda _label: None
        return service

    def _write(self, **disk: object) -> SlotDetailCacheEntry:
        service = self._service()
        slot = self._slot(**disk)
        summary = service._merge_smart_summary(slot, service._build_quantastor_smart_summary(slot))
        entry = service._build_slot_detail_entry(slot, smart_summary=summary)
        self.assertIsNotNone(entry)
        service._persist_slot_detail_entries([entry])
        return entry

    def _store(self, smart_fields: dict) -> None:
        SlotDetailStore(str(self.path)).save_entries([SlotDetailCacheEntry(
            system_id="synthetic", enclosure_id="enc-1", slot=0, identifiers=[self.SERIAL],
            slot_fields={"serial": self.SERIAL}, smart_fields=smart_fields,
        )])

    def _readers(self) -> dict[str, SmartSummaryView | None]:
        return {
            "rebuild": self._service()._build_persisted_smart_summary(self._slot()),
            "no-layout": self._service().get_cached_slot_smart_summary_without_layout(0, "enc-1"),
            "no-layout-batch": self._service().get_cached_slot_smart_summaries_without_layout(
                [0, 1], "enc-1",
            ).get(0),
        }

    def _lifetime(self, summary: SmartSummaryView | None) -> tuple:
        self.assertIsNotNone(summary)
        return summary.power_on_hours, summary.power_on_days, summary.unread_fields

    def test_layout_unavailable_reader_refuses_a_stored_negative_power_on_time(self) -> None:
        for fields, expected in (
            ({"power_on_hours": -1, "power_on_days": -1}, (None, None, self.UNREAD)),
            ({"power_on_hours": -1}, (None, None, ["power_on_hours"])),
            ({"power_on_days": -1}, (None, None, ["power_on_days"])),
        ):
            self._store({"available": True, **fields})
            for reader in ("no-layout", "no-layout-batch"):
                with self.subTest(fields=fields, reader=reader):
                    self.assertEqual(self._lifetime(self._readers()[reader]), expected)

    def test_layout_unavailable_reader_keeps_real_and_zero_power_on_time(self) -> None:
        for hours, days in ((0, 0), (48, 2), (321, 13)):
            self._store({"available": True, "power_on_hours": hours, "power_on_days": days})
            for reader in ("no-layout", "no-layout-batch"):
                with self.subTest(hours=hours, reader=reader):
                    self.assertEqual(self._lifetime(self._readers()[reader]), (hours, days, []))

    def test_refused_power_on_time_beside_a_stable_field_is_written_and_read_back(self) -> None:
        entry = self._write(smartHealthTest="PASSED", powerOnHours=-1, firmwareVersion="FW01")
        self.assertEqual(entry.smart_fields,
                         {"firmware_version": "FW01", "unread_fields": self.UNREAD, "available": True})
        self.assertIsNotNone(entry.smart_updated_at)
        self.assertFalse(entry.smart_stale)
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual(self._lifetime(summary), (None, None, self.UNREAD))
                self.assertEqual(summary.firmware_version, "FW01")

    def test_refused_power_on_time_alone_is_written_and_read_back(self) -> None:
        entry = self._write(powerOnHours=-1)
        self.assertEqual(entry.smart_fields, {"unread_fields": self.UNREAD, "available": False})
        self.assertIsNotNone(entry.smart_updated_at)
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual(self._lifetime(summary), (None, None, self.UNREAD))

    def test_a_real_reading_after_a_refusal_clears_the_stored_marker(self) -> None:
        self._write(smartHealthTest="PASSED", powerOnHours=-1, firmwareVersion="FW01")
        entry = self._write(smartHealthTest="PASSED", powerOnHours=100, firmwareVersion="FW01")
        self.assertNotIn("unread_fields", entry.smart_fields)
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual(self._lifetime(summary), (100, 4, []))

    def test_a_refusal_after_a_real_reading_replaces_the_stored_lifetime(self) -> None:
        self._write(smartHealthTest="PASSED", powerOnHours=100, firmwareVersion="FW01")
        entry = self._write(smartHealthTest="PASSED", powerOnHours=-1, firmwareVersion="FW01")
        self.assertNotIn("power_on_hours", entry.smart_fields)
        self.assertFalse(entry.smart_stale)
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual(self._lifetime(summary), (None, None, self.UNREAD))

    def test_only_refused_lifetime_names_are_written(self) -> None:
        slot = self._slot()
        summary = SmartSummaryView(available=True, firmware_version="FW01", power_on_hours=48, power_on_days=2,
                                   unread_fields=["power_on_hours", "temperature_c"])
        entry = self._service()._build_slot_detail_entry(slot, smart_summary=summary)
        self.assertNotIn("unread_fields", entry.smart_fields)
        summary = SmartSummaryView(available=True, firmware_version="FW01",
                                   unread_fields=["temperature_c", "power_on_hours"])
        entry = self._service()._build_slot_detail_entry(slot, smart_summary=summary)
        self.assertEqual(entry.smart_fields.get("unread_fields"), ["power_on_hours"])

    def test_a_stored_name_with_a_real_value_beside_it_is_dropped(self) -> None:
        self._store({"available": True, "power_on_hours": 48, "unread_fields": ["power_on_hours", "temperature_c"]})
        # Only the layout-backed rebuild works out days from hours, as on main.
        expected = {"rebuild": (48, 2, []), "no-layout": (48, None, []), "no-layout-batch": (48, None, [])}
        for reader, summary in self._readers().items():
            with self.subTest(reader=reader):
                self.assertEqual(self._lifetime(summary), expected[reader])

    def test_the_marker_survives_a_separate_process(self) -> None:
        self._write(smartHealthTest="PASSED", powerOnHours=-1, firmwareVersion="FW01")
        code = (
            "import json, sys\n"
            "from app.config import SystemConfig\n"
            "from app.models.domain import SlotView\n"
            "from app.services.inventory import InventoryService\n"
            "from app.services.slot_detail_store import SlotDetailStore\n"
            "service = object.__new__(InventoryService)\n"
            "service.system = SystemConfig(id='synthetic')\n"
            "service.slot_detail_store = SlotDetailStore(sys.argv[1])\n"
            "slot = SlotView(slot=0, slot_label='00', row_index=0, column_index=0, enclosure_id='enc-1',"
            " present=True, serial=sys.argv[2])\n"
            "summary = service._build_persisted_smart_summary(slot)\n"
            "print(json.dumps([summary.power_on_hours, summary.power_on_days, summary.unread_fields,"
            " summary.firmware_version]))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.path), self.SERIAL],
            cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [None, None, self.UNREAD, "FW01"])


if __name__ == "__main__":
    unittest.main()
