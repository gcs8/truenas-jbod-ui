"""SMART and NVMe JSON summaries: unexpected shapes stay unknown (#1221).

A decoded payload that is not an object, a value of the wrong JSON type, a
boolean in a numeric field, or a non-finite or negative counter must never
raise through the typed summary boundary and must never become a reading.
Valid payloads are the controls: they still produce the same readings.
"""

import json
import unittest
from decimal import Decimal

from app.models.domain import SlotView
from app.services.inventory import InventoryService
from app.services.parsers import (
    parse_nvme_id_ctrl_summary,
    parse_nvme_id_ns_summary,
    parse_nvme_smart_log_summary,
    parse_smartctl_summary,
)
from app.services.ssh_probe import SSHCommandResult

SMART_PARSE_FAILED = "SMART JSON parsing failed."

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
        self.assertEqual(parse_nvme_id_ns_summary('{"eui64": "00000100000002"}')["namespace_eui64"], "eui.00000100000002")

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


if __name__ == "__main__":
    unittest.main()
