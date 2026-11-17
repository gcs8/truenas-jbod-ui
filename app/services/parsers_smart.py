"""SMART and NVMe summary readers: smartctl JSON and text, nvme smart-log, id-ctrl and id-ns.

Moved out of ``app.services.parsers``, which re-exports every name defined here.
This module must not import ``app.services.parsers``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from decimal import Decimal, DecimalException
from typing import Any

from app.config import normalize_text

HEX_VALUE_REGEX = re.compile(r"^(?:0x)?(?P<value>[0-9a-fA-F]+)$")
HEX_IDENTIFIER_ERROR_SPLIT_REGEX = re.compile(r"(?i)error:")


def normalize_hex_identifier(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = HEX_IDENTIFIER_ERROR_SPLIT_REGEX.split(value.strip(), maxsplit=1)[0].strip()
    if not cleaned:
        return None
    match = HEX_VALUE_REGEX.match(cleaned)
    if not match:
        return None
    normalized = match.group("value").lower().lstrip("0")
    return normalized or "0"


def format_hex_identifier(value: str | None) -> str | None:
    normalized = normalize_hex_identifier(value)
    if normalized is not None:
        return f"0x{normalized}"
    return None


def _coerce_non_negative_int(value: Any) -> int | None:
    # Exact type: a JSON boolean is not a reading, and bool subclasses int.
    if type(value) is int and value >= 0:
        return value
    return None


def _json_text(value: Any) -> str | None:
    """Reported JSON text, or None when the field is not a string."""
    return normalize_text(value) if isinstance(value, str) else None


# SMART summary fields are tri-state: a reading, absent (the source key was not
# reported), or not read (the key was reported but not in its documented
# shape). The readers below return the reading or None and add the summary
# field names they could not read to ``unread``; a fallback source is consulted
# only while its field is absent, never in place of one that was not read.
def _reported_object(container: dict[str, Any], key: str, unread: set[str], *fields: str) -> dict[str, Any]:
    """The object at ``key``; a reported non-object marks ``fields`` not read."""
    if key not in container:
        return {}
    value = container[key]
    if isinstance(value, dict):
        return value
    unread.update(fields)
    return {}


def _reported(container: dict[str, Any], key: str, admit: Any, unread: set[str], field: str) -> Any:
    """``admit(value)`` for a reported key; a refused value marks ``field`` not read."""
    if key not in container:
        return None
    value = admit(container[key])
    if value is None:
        unread.add(field)
    return value


def _reported_text(container: dict[str, Any], key: str, unread: set[str], field: str) -> str | None:
    """Reported text; a non-string or blank value is not read."""
    return _reported(container, key, _json_text, unread, field)


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _load_json_object(output: str) -> dict[str, Any] | None:
    """Decode a JSON object, or None for any other root or undecodable input.

    ``ValueError`` covers ``JSONDecodeError`` and an integer literal past the
    interpreter's digit limit; deep nesting raises ``RecursionError``.
    """
    try:
        payload = json.loads(output)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


# A byte total at or above 2**64 is not a plausible drive counter. Refusing it
# keeps the annualised and endurance estimates (float division) from
# overflowing, and Decimal-to-int conversion from building an unbounded integer.
_BYTE_COUNTER_LIMIT = 2**64
# Largest gigabytes_processed value whose byte total stays under the limit.
_SCSI_GIGABYTES_LIMIT = Decimal(_BYTE_COUNTER_LIMIT) / Decimal(1_000_000_000)


def _bounded_byte_count(byte_count: int | None) -> int | None:
    if byte_count is None or not 0 <= byte_count < _BYTE_COUNTER_LIMIT:
        return None
    return byte_count


def _nvme_data_units_to_bytes(value: Any) -> int | None:
    units = _coerce_non_negative_int(value)
    if units is None:
        return None
    # NVMe SMART "data units" are reported in units of 1000 * 512 bytes.
    return _bounded_byte_count(units * 512_000)


def _scsi_gigabytes_processed_to_bytes(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text:
        return None
    try:
        gigabytes = Decimal(text)
        # Validate before arithmetic: an exponent past the Decimal context
        # overflows on multiplication, and a tiny negative value rounds to -0.
        # Non-finite, signed (including -0) or implausibly large counters stay unknown.
        if not gigabytes.is_finite() or gigabytes.is_signed() or gigabytes >= _SCSI_GIGABYTES_LIMIT:
            return None
        return _bounded_byte_count(int(gigabytes * Decimal(1_000_000_000)))
    except (DecimalException, ValueError, OverflowError):
        return None


def _annualize_bytes(
    byte_count: int | None,
    power_on_hours: int | None,
    *,
    minimum_hours: int = 24 * 30,
) -> int | None:
    if byte_count is None or not isinstance(power_on_hours, int) or power_on_hours < minimum_hours:
        return None
    return int(byte_count * 24 * 365 / power_on_hours)


# NVMe reports composite temperature and its thresholds as 16-bit Kelvin words.
_NVME_KELVIN_LIMIT = 0xFFFF


# A drive temperature reading in Celsius is an integer in this range; any other
# value is not read. The bound also keeps every consumer's float conversion safe.
_CELSIUS_MIN, _CELSIUS_MAX = -100, 1000


def _json_celsius(value: Any) -> int | None:
    return value if type(value) is int and _CELSIUS_MIN <= value <= _CELSIUS_MAX else None


def _kelvin_to_celsius(value: Any) -> int | None:
    kelvin = _coerce_non_negative_int(value)
    # Bounded before float arithmetic: an oversized integer cannot become a float.
    if kelvin is None or kelvin == 0 or kelvin > _NVME_KELVIN_LIMIT:
        return None
    return _json_celsius(int(round(kelvin - 273.15)))


def _reported_kelvin(container: dict[str, Any], key: str, unread: set[str], field: str) -> int | None:
    """An NVMe Kelvin word in Celsius; 0 K means "not reported" (absent)."""
    if container.get(key) == 0 and type(container.get(key)) is int:
        return None
    return _reported(container, key, _kelvin_to_celsius, unread, field)


def _format_nvme_version(value: Any) -> str | None:
    # The version word is 32 bits wide; a wider value is not a version.
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        return None
    major = (value >> 16) & 0xFFFF
    minor = (value >> 8) & 0xFF
    tertiary = value & 0xFF
    if tertiary:
        return f"{major}.{minor}.{tertiary}"
    return f"{major}.{minor}"


# nvme-cli reports a namespace EUI-64 as 16 hex digits and an NGUID as 32; the
# optional "eui." prefix is the form this formatter publishes.
_NVME_EUI64_REGEX = re.compile(r"(?:[eE][uU][iI]\.)?([0-9a-fA-F]{16})")
_NVME_NGUID_REGEX = re.compile(r"[0-9a-fA-F]{32}")


def _format_nvme_eui64(value: Any) -> str | None:
    match = _NVME_EUI64_REGEX.fullmatch(value.strip()) if isinstance(value, str) else None
    return f"eui.{match.group(1).lower()}" if match else None


def _format_nvme_nguid(value: Any) -> str | None:
    match = _NVME_NGUID_REGEX.fullmatch(value.strip()) if isinstance(value, str) else None
    return match.group(0).lower() if match else None


def _ata_attributes_unreadable(payload: dict[str, Any]) -> bool:
    """True when the attribute table was reported but a row in it cannot be read.

    Such a row may be any attribute, so an attribute the table does not
    otherwise report is not read rather than absent.
    """
    block = payload.get("ata_smart_attributes")
    if block is None and "ata_smart_attributes" not in payload:
        return False
    if not isinstance(block, dict):
        return True
    if "table" not in block:
        return False
    table = block["table"]
    if not isinstance(table, list):
        return True
    # ATA attribute numbers are 1 to 255; any other id is not an identity.
    return any(
        not isinstance(entry, dict) or type(entry.get("id")) is not int or not 1 <= entry["id"] <= 255
        for entry in table
    )


# smartctl's raw string for these attributes is the decimal count, optionally
# followed by a parenthesised detail such as "(Min/Max 20/45)". ASCII digits
# only, at most 20 of them (a 64-bit count), so conversion cannot fail.
_ATA_RAW_STRING_REGEX = re.compile(r"([0-9]{1,20})(?: \(.*\))?")


def _ata_raw_count(raw: Any) -> int | None:
    """An attribute's raw count in a documented form, or None.

    smartctl reports ``raw`` as ``{"value": <int>, "string": <text>}``; a bare
    integer or string is the older compatibility form. When ``value`` is
    reported it decides: a malformed ``value`` is not replaced by the string.
    """
    if isinstance(raw, dict):
        if "value" in raw:
            return _coerce_non_negative_int(raw["value"])
        # ``string`` is text; the bare-integer compatibility form is not a member.
        string = raw.get("string")
        raw = string if isinstance(string, str) else None
    if type(raw) is int:
        return raw if raw >= 0 else None
    if isinstance(raw, str):
        match = _ATA_RAW_STRING_REGEX.fullmatch(raw.strip())
        return int(match.group(1)) if match else None
    return None


def _iter_ata_attribute_entries(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    table = (
        payload.get("ata_smart_attributes", {}).get("table")
        if isinstance(payload.get("ata_smart_attributes"), dict)
        else None
    )
    if not isinstance(table, list):
        return
    # Only rows with an exact integer id are attributes: a list or object id
    # cannot be looked up, and a float or boolean id is not an attribute number.
    yield from (
        entry for entry in table if isinstance(entry, dict) and type(entry.get("id")) is int
    )


def _extract_ata_attribute_raw_value(
    payload: dict[str, Any],
    *attribute_ids: int,
    unread: set[str] | None = None,
    field: str = "",
) -> int | None:
    """The first matching attribute's raw count; a malformed one is not read."""
    unread = unread if unread is not None else set()
    entry = _extract_ata_attribute_entry(payload, *attribute_ids)
    if entry is None:
        if _ata_attributes_unreadable(payload):
            unread.add(field)
        return None
    value = _ata_raw_count(entry["raw"]) if "raw" in entry else None
    if value is None:
        unread.add(field)
    return value


def _extract_ata_attribute_entry(payload: dict[str, Any], *attribute_ids: int) -> dict[str, Any] | None:
    attribute_id_set = set(attribute_ids)
    for entry in _iter_ata_attribute_entries(payload):
        if entry.get("id") in attribute_id_set:
            return entry
    return None


_ATA_UNIT_SCALES = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
# The unit in a drive-database attribute name such as Host_Writes_32MiB or
# Lifetime_Writes_GiB: an optional ASCII multiplier (no leading zero, at most
# six digits) and a binary unit. Any other unit-like ending is not read.
_ATA_UNIT_NAME_REGEX = re.compile(r"([1-9][0-9]{0,5})?(KiB|MiB|GiB|TiB)")
_ATA_UNIT_LIKE_REGEX = re.compile(r".*[kmgtpe]i?b", re.IGNORECASE)
_ATA_SECTOR_UNIT = 0


def _ata_attribute_unit_bytes(name: str) -> int | None:
    """Bytes per raw count named by the attribute, ``_ATA_SECTOR_UNIT`` for a
    sector count (no unit in the name), or None for an unreadable unit."""
    head, separator, tail = name.rpartition("_")
    if not _ATA_UNIT_LIKE_REGEX.fullmatch(tail):
        return _ATA_SECTOR_UNIT
    match = _ATA_UNIT_NAME_REGEX.fullmatch(tail) if separator and head else None
    if match is None:
        return None
    return int(match.group(1) or 1) * _ATA_UNIT_SCALES[match.group(2)]


def _ata_attribute_raw_value_to_bytes(
    entry: dict[str, Any],
    sector_size: int | None,
    unread: set[str] | None = None,
    field: str = "",
) -> int | None:
    unread = unread if unread is not None else set()
    count = _ata_raw_count(entry["raw"]) if "raw" in entry else None
    unit_bytes: int | None = _ATA_SECTOR_UNIT
    if "name" in entry:
        # A reported name decides the unit; one that cannot be read is not
        # replaced by the sector reading an absent name keeps.
        name = _json_text(entry["name"])
        unit_bytes = _ata_attribute_unit_bytes(name) if name else None
    if unit_bytes == _ATA_SECTOR_UNIT:
        # A sector count without an admitted sector size has no byte total.
        unit_bytes = sector_size
    total = _bounded_byte_count(count * unit_bytes) if count is not None and unit_bytes is not None else None
    if total is None:
        unread.add(field)
    return total


def _iter_ata_device_stat_entries(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    ata_device_statistics = (
        payload.get("ata_device_statistics")
        if isinstance(payload.get("ata_device_statistics"), dict)
        else {}
    )
    pages = ata_device_statistics.get("pages") if isinstance(ata_device_statistics.get("pages"), list) else []
    for page in pages:
        if not isinstance(page, dict):
            continue
        raw_table = page.get("table")
        table = raw_table if isinstance(raw_table, list) else []
        yield from (entry for entry in table if isinstance(entry, dict))


def _ata_device_stats_unreadable(payload: dict[str, Any]) -> bool:
    """True when device statistics were reported but part of them cannot be read."""
    block = payload.get("ata_device_statistics")
    if block is None and "ata_device_statistics" not in payload:
        return False
    if not isinstance(block, dict):
        return True
    if "pages" not in block:
        return False
    pages = block["pages"]
    if not isinstance(pages, list):
        return True
    for page in pages:
        if not isinstance(page, dict):
            return True
        if "table" not in page:
            continue
        table = page["table"]
        # A row without a non-blank name may be any statistic.
        if not isinstance(table, list) or any(
            not isinstance(entry, dict) or _json_text(entry.get("name")) is None for entry in table
        ):
            return True
    return False


def _extract_ata_device_stat_int(
    payload: dict[str, Any],
    stat_name: str,
    unread: set[str] | None = None,
    field: str = "",
) -> int | None:
    """The named statistic's count; smartctl omits ``value`` for one the drive
    marks invalid (absent), and a reported malformed value is not read."""
    unread = unread if unread is not None else set()
    for entry in _iter_ata_device_stat_entries(payload):
        if _json_text(entry.get("name")) != stat_name:
            continue
        return _reported(entry, "value", _coerce_non_negative_int, unread, field)
    if _ata_device_stats_unreadable(payload):
        unread.add(field)
    return None


def _extract_ata_device_stat_bytes(
    payload: dict[str, Any],
    stat_name: str,
    sector_size: int | None,
    unread: set[str] | None = None,
    field: str = "",
) -> int | None:
    unread = unread if unread is not None else set()
    value = _extract_ata_device_stat_int(payload, stat_name, unread, field)
    if value is None:
        return None
    # A sector count without an admitted sector size has no byte total.
    total = _bounded_byte_count(value * sector_size) if sector_size is not None else None
    if total is None:
        unread.add(field)
    return total


def _parse_enabled_disabled(value: str | None) -> bool | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered.startswith("enabled"):
        return True
    if lowered.startswith("disabled"):
        return False
    return None


SMARTCTL_CACHE_TOGGLE_PREFIXES = {
    "Read Cache is:": "read",
    "Rd look-ahead is:": "read",
    "Writeback Cache is:": "writeback",
    "Write cache is:": "writeback",
}


# The summary message when every reported field was refused: the source did
# return fields, so "no fields were returned" would describe an absence.
SMART_SUMMARY_UNREAD_MESSAGE = "SMART summary fields were returned for this disk but could not be read."


def _finalize_summary(
    data: dict[str, Any],
    unavailable_message: str,
    *,
    availability_values: tuple[Any, ...] | None = None,
    available_message: str | None = None,
    unread: set[str] | None = None,
) -> dict[str, Any]:
    values = availability_values if availability_values is not None else tuple(data.values())
    available = any(value is not None for value in values)
    if unread is None:
        # Readers that do not track refused fields keep their output shape.
        return {"available": available, **data, "message": available_message if available else unavailable_message}
    # Only fields left without a reading are "not read"; a later source that
    # read the field supersedes the refusal of an earlier one.
    unread_fields = sorted(name for name in unread if name in data and data[name] is None)
    if not available and unread_fields:
        unavailable_message = SMART_SUMMARY_UNREAD_MESSAGE
    return {
        "available": available,
        **data,
        "unread_fields": unread_fields,
        "message": available_message if available else unavailable_message,
    }


_NVME_HEALTH_FIELDS = (
    "available_spare_percent", "available_spare_threshold_percent", "endurance_used_percent",
    "bytes_read", "bytes_written", "media_errors", "unsafe_shutdowns",
)


def _derived_unread(unread: set[str], field: str, value: Any, inputs: dict[str, Any]) -> None:
    """A derived value is not read when it is missing because an input was not
    read; with any input simply absent it is absent too."""
    if (
        value is None
        and any(name in unread for name in inputs)
        and all(input_value is not None or name in unread for name, input_value in inputs.items())
    ):
        unread.add(field)


def _extract_smartctl_traffic_and_wear(
    payload: dict[str, Any],
    power_on_hours: int | None,
    sector_size_for_ata: int | None,
    unread: set[str] | None = None,
) -> dict[str, int | None]:
    unread = unread if unread is not None else set()
    nvme_health = _reported_object(payload, "nvme_smart_health_information_log", unread, *_NVME_HEALTH_FIELDS)
    available_spare_percent = _reported(
        nvme_health, "available_spare", _coerce_non_negative_int, unread, "available_spare_percent",
    )
    available_spare_threshold_percent = _reported(
        nvme_health, "available_spare_threshold", _coerce_non_negative_int, unread, "available_spare_threshold_percent",
    )
    endurance_used_percent = _reported(
        nvme_health, "percentage_used", _coerce_non_negative_int, unread, "endurance_used_percent",
    )
    bytes_read = _reported(nvme_health, "data_units_read", _nvme_data_units_to_bytes, unread, "bytes_read")
    bytes_written = _reported(nvme_health, "data_units_written", _nvme_data_units_to_bytes, unread, "bytes_written")
    media_errors = _reported(nvme_health, "media_errors", _coerce_non_negative_int, unread, "media_errors")
    unsafe_shutdowns = _reported(nvme_health, "unsafe_shutdowns", _coerce_non_negative_int, unread, "unsafe_shutdowns")

    # Each lower source is read only while the field is still absent: a
    # source that reported the field in an unreadable shape stops the chain.
    scsi_error_counter_log = _reported_object(payload, "scsi_error_counter_log", unread, "bytes_read", "bytes_written")

    def lower_byte_sources(
        value: int | None, byte_field: str, direction: str, stat_name: str, attribute_id: int,
    ) -> int | None:
        if value is None and byte_field not in unread:
            log = _reported_object(scsi_error_counter_log, direction, unread, byte_field)
            value = _reported(log, "gigabytes_processed", _scsi_gigabytes_processed_to_bytes, unread, byte_field)
        if value is None and byte_field not in unread:
            value = _extract_ata_device_stat_bytes(payload, stat_name, sector_size_for_ata, unread, byte_field)
        if value is None and byte_field not in unread:
            entry = _extract_ata_attribute_entry(payload, attribute_id)
            if entry is not None:
                value = _ata_attribute_raw_value_to_bytes(entry, sector_size_for_ata, unread, byte_field)
            elif _ata_attributes_unreadable(payload):
                unread.add(byte_field)
        return value

    bytes_read = lower_byte_sources(bytes_read, "bytes_read", "read", "Logical Sectors Read", 242)
    bytes_written = lower_byte_sources(bytes_written, "bytes_written", "write", "Logical Sectors Written", 241)
    if available_spare_percent is None and "available_spare_percent" not in unread:
        available_spare_percent = _extract_ata_attribute_raw_value(
            payload, 232, 169, unread=unread, field="available_spare_percent",
        )
    if endurance_used_percent is None and "endurance_used_percent" not in unread:
        endurance_used_percent = _extract_ata_device_stat_int(
            payload, "Percentage Used Endurance Indicator", unread, "endurance_used_percent",
        )
    endurance_remaining_percent = (
        max(0, 100 - endurance_used_percent) if endurance_used_percent is not None else None
    )

    annualized_bytes_read = _annualize_bytes(bytes_read, power_on_hours)
    annualized_bytes_written = _annualize_bytes(bytes_written, power_on_hours)
    estimated_lifetime_bytes_written = (
        int(bytes_written * 100 / endurance_used_percent)
        if bytes_written is not None
        and endurance_used_percent is not None
        and endurance_used_percent > 0
        else None
    )
    estimated_remaining_bytes_written = (
        max(estimated_lifetime_bytes_written - bytes_written, 0)
        if estimated_lifetime_bytes_written is not None and bytes_written is not None
        else None
    )
    _derived_unread(
        unread, "endurance_remaining_percent", endurance_remaining_percent,
        {"endurance_used_percent": endurance_used_percent},
    )
    _derived_unread(
        unread, "annualized_bytes_read", annualized_bytes_read,
        {"bytes_read": bytes_read, "power_on_hours": power_on_hours},
    )
    _derived_unread(
        unread, "annualized_bytes_written", annualized_bytes_written,
        {"bytes_written": bytes_written, "power_on_hours": power_on_hours},
    )
    _derived_unread(
        unread, "estimated_lifetime_bytes_written", estimated_lifetime_bytes_written,
        {"bytes_written": bytes_written, "endurance_used_percent": endurance_used_percent},
    )
    _derived_unread(
        unread, "estimated_remaining_bytes_written", estimated_remaining_bytes_written,
        {"bytes_written": bytes_written, "endurance_used_percent": endurance_used_percent},
    )
    return {
        "available_spare_percent": available_spare_percent,
        "available_spare_threshold_percent": available_spare_threshold_percent,
        "endurance_used_percent": endurance_used_percent,
        "endurance_remaining_percent": endurance_remaining_percent,
        "bytes_read": bytes_read,
        "bytes_written": bytes_written,
        "annualized_bytes_read": annualized_bytes_read,
        "annualized_bytes_written": annualized_bytes_written,
        "estimated_lifetime_bytes_written": estimated_lifetime_bytes_written,
        "estimated_remaining_bytes_written": estimated_remaining_bytes_written,
        "media_errors": media_errors,
        "unsafe_shutdowns": unsafe_shutdowns,
    }


_SMART_LATEST_TEST_FIELDS = ("last_test_type", "last_test_status", "last_test_lifetime_hours")
# smartctl numbers the SCSI self-test log entries scsi_self_test_0 to _19,
# newest first. Canonical ASCII decimal only, bounded before conversion.
_SCSI_SELF_TEST_INDEX_REGEX = re.compile(r"0|[1-9][0-9]{0,5}")


def _reported_test_text(
    container: dict[str, Any], key: str, unread: set[str], field: str, *, allow_text: bool,
) -> str | None:
    """A self-test type or status: an object's ``string`` (else ``name``), or
    plain text where that older form is accepted."""
    if key not in container:
        return None
    value = container[key]
    if isinstance(value, dict):
        text = _reported_text(value, "string", unread, field)
        if text is None and field not in unread:
            text = _reported_text(value, "name", unread, field)
        if text is None:
            # An object without its text is a reported value that was not read.
            unread.add(field)
        return text
    if allow_text:
        return _reported_text(container, key, unread, field)
    unread.add(field)
    return None


def _extract_smartctl_latest_test(
    payload: dict[str, Any],
    unread: set[str] | None = None,
) -> tuple[str | None, str | None, int | None]:
    unread = unread if unread is not None else set()
    latest_test_type: str | None = None
    latest_test_status: str | None = None
    latest_test_lifetime_hours: int | None = None
    scsi_tests: list[tuple[int, dict[str, Any]]] = []
    for key, value in payload.items():
        if not key.startswith("scsi_self_test_"):
            continue
        suffix = key.removeprefix("scsi_self_test_")
        if not (_SCSI_SELF_TEST_INDEX_REGEX.fullmatch(suffix) and isinstance(value, dict)):
            # An entry that cannot be ordered may be the newest one, so no
            # entry can be reported as the latest test.
            unread.update(_SMART_LATEST_TEST_FIELDS)
            return None, None, None
        scsi_tests.append((int(suffix), value))

    if scsi_tests:
        _, latest_test = min(scsi_tests, key=lambda item: item[0])
        latest_test_type = _reported_test_text(latest_test, "code", unread, "last_test_type", allow_text=False)
        latest_test_status = _reported_test_text(latest_test, "result", unread, "last_test_status", allow_text=False)
        lifetime = _reported_object(latest_test, "power_on_time", unread, "last_test_lifetime_hours")
        latest_test_lifetime_hours = _reported(
            lifetime, "hours", _coerce_non_negative_int, unread, "last_test_lifetime_hours",
        )
    else:
        ata_log = _reported_object(payload, "ata_smart_self_test_log", unread, *_SMART_LATEST_TEST_FIELDS)
        standard = _reported_object(ata_log, "standard", unread, *_SMART_LATEST_TEST_FIELDS)
        table = standard.get("table", [])
        if not isinstance(table, list) or (table and not isinstance(table[0], dict)):
            unread.update(_SMART_LATEST_TEST_FIELDS)
        elif table:
            latest_test = table[0]
            latest_test_type = _reported_test_text(latest_test, "type", unread, "last_test_type", allow_text=True)
            latest_test_status = _reported_test_text(latest_test, "status", unread, "last_test_status", allow_text=True)
            if isinstance(latest_test.get("lifetime_hours"), dict):
                # The object form carries its value in ``hours``; without it the
                # reported lifetime was not read.
                lifetime = {"hours": None, **latest_test["lifetime_hours"]}
                latest_test_lifetime_hours = _reported(
                    lifetime, "hours", _coerce_non_negative_int, unread, "last_test_lifetime_hours",
                )
            else:
                latest_test_lifetime_hours = _reported(
                    latest_test, "lifetime_hours", _coerce_non_negative_int, unread, "last_test_lifetime_hours",
                )

    return latest_test_type, latest_test_status, latest_test_lifetime_hours


def _reported_sas_address(container: dict[str, Any], key: str, unread: set[str], field: str) -> str | None:
    return _reported(container, key, lambda value: format_hex_identifier(_json_text(value)), unread, field)


def _extract_smartctl_sas_phy(
    payload: dict[str, Any],
    unread: set[str] | None = None,
) -> tuple[str | None, str | None, str | None] | None:
    unread = unread if unread is not None else set()
    sas_port_phys: list[dict[str, Any]] = []
    for key, value in payload.items():
        if not key.startswith("scsi_sas_port_"):
            continue
        phys = [phy for subkey, phy in value.items() if subkey.startswith("phy_")] if isinstance(value, dict) else None
        if phys is None or not all(isinstance(phy, dict) for phy in phys):
            # A port or PHY that cannot be read may be the attached one, so no
            # PHY is reported as the disk's, and no other source stands in.
            unread.update(("sas_address", "attached_sas_address", "negotiated_link_rate"))
            return None, None, None
        sas_port_phys.extend(phys)

    selected_phy = next(
        (
            phy
            for phy in sas_port_phys
            if format_hex_identifier(_json_text(phy.get("attached_sas_address"))) not in {None, "0x0"}
            or _json_text(phy.get("attached_device_type")) not in {None, "no device attached"}
        ),
        sas_port_phys[0] if sas_port_phys else None,
    )
    if not selected_phy:
        return None
    return (
        _reported_sas_address(selected_phy, "sas_address", unread, "sas_address"),
        _reported_sas_address(selected_phy, "attached_sas_address", unread, "attached_sas_address"),
        _reported_text(selected_phy, "negotiated_logical_link_rate", unread, "negotiated_link_rate"),
    )


# Retention caps for the optional per-disk counter collections. SMART output
# is already bounded at 4 MiB per command, but that still admits tens of
# thousands of rows per disk, retained for every disk in the SMART cache. The
# caps are internal and provisional:
# - SAS: 32 distinct (port, PHY) identities is a provisional retention budget,
#   not a claim about a universal hardware limit. Overflow makes this optional
#   collection unavailable instead of presenting an apparently complete prefix.
# - SATA: GP log 0x11 is one 512-byte page and smartctl reads one page; each
#   counter needs at least 4 bytes after the 4-byte header, so a device can
#   report at most 126 distinct identifiers. 128 covers the whole page.
# Exceeding a cap refuses that optional collection (None) rather than showing
# a prefix that looks complete; health/temperature/identity are unaffected.
SMART_SAS_PORT_ROW_CAP = 32
SMART_SATA_PHY_EVENT_CAP = 128

_SMART_ROW_CONFLICT = object()
_SMART_ROW_MISSING = object()


class _BoundedIdentityRows:
    """Retain at most ``cap`` distinct identities in first-reported order.

    Admission happens before any row list exists, so repeated input never
    allocates one retained row per repeat. An identical repeat of an identity
    collapses to one row. A repeat with different content makes that identity
    unreported, because the log cannot say which value is true. More than
    ``cap`` distinct identities refuses the whole collection.
    """

    __slots__ = ("_cap", "_rows", "_overflowed")

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._rows: dict[Any, Any] = {}
        self._overflowed = False

    def admit(self, identity: Any, row: dict[str, Any]) -> bool:
        """Record one reported row; False once the identity cap is exceeded."""
        if self._overflowed:
            return False
        existing = self._rows.get(identity, _SMART_ROW_MISSING)
        if existing is _SMART_ROW_MISSING:
            if len(self._rows) >= self._cap:
                self._overflowed = True
                return False
            self._rows[identity] = row
        elif existing is not _SMART_ROW_CONFLICT and existing != row:
            self._rows[identity] = _SMART_ROW_CONFLICT
        return True

    def rows(self) -> list[dict[str, Any]] | None:
        if self._overflowed:
            return None
        retained = [row for row in self._rows.values() if row is not _SMART_ROW_CONFLICT]
        return retained or None


SMART_COUNTER_METADATA_MAX_CHARS = 256


def _smart_counter_metadata(value: Any) -> str | None:
    # Bound newly retained reported text before normalization/copying. Preserve
    # measured counters if descriptions are oversized or not valid Unicode.
    if not isinstance(value, str) or len(value) > SMART_COUNTER_METADATA_MAX_CHARS:
        return None
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        return None
    return normalize_text(value)


def _sas_port_row(port: int, phy_id: int, phy: dict[str, Any]) -> dict[str, Any]:
    # Optional metadata is reported text, never coerced from malformed JSON
    # values. Keep this admission local to the port-row readers.
    row = {
        "port": port,
        "phy": phy_id,
        "attached_type": _smart_counter_metadata(phy.get("attached_device_type")),
        "attached_reason": _smart_counter_metadata(phy.get("attached_reason")),
        "sas_address": format_hex_identifier(_smart_counter_metadata(phy.get("sas_address"))),
        "attached_sas_address": format_hex_identifier(_smart_counter_metadata(phy.get("attached_sas_address"))),
        "negotiated_link_rate": _smart_counter_metadata(phy.get("negotiated_logical_link_rate")),
    }
    for counter in (
        "invalid_dword_count", "running_disparity_error_count",
        "loss_of_dword_synchronization_count", "phy_reset_problem_count",
    ):
        count = phy.get(counter)
        # Missing/invalid values are not measured zeros. FFFFFFFF is a valid
        # saturated total; never wrap, sum, reset or infer it.
        row[counter] = count if type(count) is int and 0 <= count <= 0xFFFFFFFF else None
    return row


def _extract_smartctl_sas_ports(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Retain per-disk PHY totals only from already-collected port-log JSON.

    smartctl numbers ``scsi_sas_port_N`` and ``phy_N`` by output position. The
    reported identity is the embedded ``relative_target_port_id`` (16-bit) and
    ``identifier`` (8-bit); a block whose identity is missing, a bool, out
    of range or otherwise malformed is unknown and is not admitted.
    """
    rows = _BoundedIdentityRows(SMART_SAS_PORT_ROW_CAP)
    for key, value in payload.items():
        if not isinstance(key, str) or re.fullmatch(r"scsi_sas_port_[0-9]+", key) is None or not isinstance(value, dict):
            continue
        port = value.get("relative_target_port_id")
        if type(port) is not int or not 0 <= port <= 65535:
            continue
        for subkey, phy in value.items():
            if not isinstance(subkey, str) or re.fullmatch(r"phy_[0-9]+", subkey) is None or not isinstance(phy, dict):
                continue
            phy_id = phy.get("identifier")
            if type(phy_id) is not int or not 0 <= phy_id <= 255:
                continue
            if not rows.admit((port, phy_id), _sas_port_row(port, phy_id, phy)):
                return None
    return rows.rows()


# smartmontools scsiprint.cpp show_sas_port_param labels. RELEASE_7_3 added a
# "count" suffix to two counter labels; both exact generations are accepted.
_SAS_PORT_TEXT_LABELS = {
    "attached device type:": "attached_device_type",
    "attached reason:": "attached_reason",
    "negotiated logical link rate:": "negotiated_logical_link_rate",
    "SAS address =": "sas_address",
    "attached SAS address =": "attached_sas_address",
    "Invalid DWORD count =": "invalid_dword_count",
    "Running disparity error count =": "running_disparity_error_count",
    "Loss of DWORD synchronization count =": "loss_of_dword_synchronization_count",
    "Phy reset problem count =": "phy_reset_problem_count",
    "Loss of DWORD synchronization =": "loss_of_dword_synchronization_count",
    "Phy reset problem =": "phy_reset_problem_count",
}


def _extract_smartctl_sas_ports_text(output: str) -> list[dict[str, Any]] | None:
    """Parse the SAS SSP port log text with the same identity and bounds as JSON.

    Identity comes from the ``relative target port id`` and ``phy identifier``
    lines. Each PHY block is admitted when the next header or the section end
    arrives, so the scan never holds more than one in-progress PHY.
    """
    rows = _BoundedIdentityRows(SMART_SAS_PORT_ROW_CAP)
    active = False
    port: int | None = None
    phy_id: int | None = None
    fields: dict[str, Any] = {}

    def flush() -> bool:
        nonlocal phy_id, fields
        admitted = True
        if port is not None and phy_id is not None:
            admitted = rows.admit((port, phy_id), _sas_port_row(port, phy_id, fields))
        phy_id, fields = None, {}
        return admitted

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "Protocol Specific port log page for SAS SSP":
            if not flush():
                return None
            active, port = True, None
            continue
        if not active:
            continue
        if line.startswith("relative target port id ="):
            if not flush():
                return None
            match = re.fullmatch(r"relative target port id =\s*([0-9]{1,5})", line)
            port = int(match.group(1)) if match and int(match.group(1)) <= 65535 else None
        elif line.startswith("phy identifier ="):
            if not flush():
                return None
            match = re.fullmatch(r"phy identifier =\s*([0-9]{1,3})", line)
            if match and int(match.group(1)) <= 255 and port is not None:
                phy_id = int(match.group(1))
        elif (label := next((key for key in _SAS_PORT_TEXT_LABELS if line.startswith(key)), None)) is not None:
            if phy_id is not None:
                field = _SAS_PORT_TEXT_LABELS[label]
                value = line[len(label):].strip()
                fields[field] = (
                    int(value) if re.fullmatch(r"[0-9]{1,10}", value) else None
                ) if field.endswith("_count") else value
        elif not line or not raw_line[:1].isspace():
            # Do not let another log's labels leak into the last disk PHY.
            if not flush():
                return None
            active, port = False, None
    if not flush():
        return None
    return rows.rows()


def _sata_phy_event_row(entry: Any) -> dict[str, Any] | None:
    """RELEASE_7_4 ataprint.cpp PrintSataPhyEventCounters JSON contract."""
    if not isinstance(entry, dict):
        return None
    event_id, size, value = entry.get("id"), entry.get("size"), entry.get("value")
    if (type(event_id) is not int or not 1 <= event_id <= 0x8FFF or event_id & ~0x8FFF
            or type(size) is not int or size not in (2, 4, 6, 8)):
        return None
    return {
        "id": event_id, "size": size,
        "name": _smart_counter_metadata(entry.get("name")),
        # A decimal string avoids rounding uint64 totals in browser JSON.parse.
        "value": str(value) if type(value) is int and 0 <= value < 1 << (8 * size) else None,
    }


def _extract_smartctl_sata_phy_events(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    block = payload.get("sata_phy_event_counters")
    table = block.get("table") if isinstance(block, dict) else None
    if not isinstance(table, list):
        return None
    rows = _BoundedIdentityRows(SMART_SATA_PHY_EVENT_CAP)
    for entry in table:
        row = _sata_phy_event_row(entry)
        if row is not None and not rows.admit(row["id"], row):
            return None
    return rows.rows()


def _extract_smartctl_sata_phy_text(output: str) -> list[dict[str, Any]] | None:
    """Parse only the documented GP log 0x11 table, never other ATA tables."""
    active = False
    table_started = False
    rows = _BoundedIdentityRows(SMART_SATA_PHY_EVENT_CAP)
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "SATA Phy Event Counters (GP Log 0x11)":
            active, table_started = True, False
            continue
        if not active:
            continue
        if re.fullmatch(r"ID\s+Size\s+Value\s+Description", line):
            table_started = True
            continue
        if not table_started and line.startswith("[Reserved:"):
            continue
        match = re.fullmatch(r"0x([0-9a-fA-F]{4})\s+([2468])\s+(\S+)\s+(.+)", line) if table_started else None
        if match is None:
            active = False
            continue
        size, token = int(match.group(2)), match.group(3)
        count = re.fullmatch(r"([0-9]{1,20})(\+?)", token)
        value = int(count.group(1)) if count else None
        if count and count.group(2) and value != (1 << (8 * size)) - 1:
            value = None
        row = _sata_phy_event_row({
            "id": int(match.group(1), 16), "size": size, "value": value, "name": match.group(4),
        })
        if row is not None and not rows.admit(row["id"], row):
            return None
    return rows.rows()


def _smartctl_threshold_celsius(value: Any) -> int | None:
    # smartctl omits unknown limits, but SCSI reports 0/255 and ATA SCT -128
    # for "not available"; none of those is a drive rating (#1103).
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 20 <= value <= 150 else None


# The documented "not available" limit values (see _smartctl_threshold_celsius).
_SMARTCTL_THRESHOLD_NOT_AVAILABLE = frozenset({0, 255, -128})


def _reported_threshold(container: dict[str, Any], key: str, unread: set[str], field: str) -> int | None:
    """A reported limit; a documented "not available" value is absent, and any
    other value that is not a plausible rating is not read."""
    if key not in container:
        return None
    value = container[key]
    if type(value) is int and value in _SMARTCTL_THRESHOLD_NOT_AVAILABLE:
        return None
    rating = _smartctl_threshold_celsius(value)
    if rating is None:
        unread.add(field)
    return rating


def _extract_smartctl_temperature_thresholds(
    payload: dict[str, Any],
    temperature: dict[str, Any],
    unread: set[str] | None = None,
) -> tuple[int | None, int | None]:
    """Return the drive's own (warning, critical) temperature ratings.

    smartctl JSON reports them per transport: NVMe WCTEMP/CCTEMP as
    ``nvme_composite_temperature_threshold`` (mirrored as ``op_limit_max`` and
    ``critical_limit_max``), ATA SCT/device statistics as the recommended
    ``op_limit_max`` and absolute ``limit_max``, and SCSI as ``drive_trip``.
    """
    unread = unread if unread is not None else set()
    nvme_thresholds = _reported_object(
        payload, "nvme_composite_temperature_threshold", unread, "warning_temperature_c", "critical_temperature_c",
    )
    warning = _reported_threshold(nvme_thresholds, "warning", unread, "warning_temperature_c")
    if warning is None and "warning_temperature_c" not in unread:
        warning = _reported_threshold(temperature, "op_limit_max", unread, "warning_temperature_c")
    critical = _reported_threshold(nvme_thresholds, "critical", unread, "critical_temperature_c")
    for field_name in ("critical_limit_max", "limit_max", "drive_trip"):
        if critical is None and "critical_temperature_c" not in unread:
            critical = _reported_threshold(temperature, field_name, unread, "critical_temperature_c")
    return warning, critical


_SMARTCTL_TEMPERATURE_FIELDS = ("temperature_c", "warning_temperature_c", "critical_temperature_c")


def _reported_namespace_eui64(payload: dict[str, Any], unread: set[str]) -> str | None:
    """The first namespace's EUI-64 from its 24-bit OUI and 40-bit extension."""
    if "nvme_namespaces" not in payload:
        return None
    namespaces = payload["nvme_namespaces"]
    if not isinstance(namespaces, list) or (namespaces and not isinstance(namespaces[0], dict)):
        unread.add("namespace_eui64")
        return None
    if not namespaces:
        return None
    eui64 = _reported_object(namespaces[0], "eui64", unread, "namespace_eui64")
    if not eui64:
        return None
    oui, ext_id = eui64.get("oui"), eui64.get("ext_id")
    if type(oui) is not int or type(ext_id) is not int or not 0 <= oui < 1 << 24 or not 0 <= ext_id < 1 << 40:
        unread.add("namespace_eui64")
        return None
    return _format_nvme_eui64(f"{oui:06x}{ext_id:010x}")


def _reported_cache_flag(payload: dict[str, Any], key: str, unread: set[str], field: str) -> bool | None:
    block = _reported_object(payload, key, unread, field)
    return _reported(block, "enabled", lambda value: value if isinstance(value, bool) else None, unread, field)


def parse_smartctl_summary(output: str) -> dict[str, Any]:
    payload = _load_json_object(output)
    if payload is None:
        # The SSH reader tries its next candidate on exactly this message.
        return {"available": False, "message": "SMART JSON parsing failed."}

    unread: set[str] = set()
    temperature = _reported_object(payload, "temperature", unread, *_SMARTCTL_TEMPERATURE_FIELDS)
    current_temperature = _reported(temperature, "current", _json_celsius, unread, "temperature_c")
    if current_temperature is None and "temperature_c" not in unread:
        environmental_reports = _reported_object(payload, "scsi_environmental_reports", unread, "temperature_c")
        environmental_temperature = _reported_object(environmental_reports, "temperature_1", unread, "temperature_c")
        current_temperature = _reported(environmental_temperature, "current", _json_celsius, unread, "temperature_c")
    power_on_time = _reported_object(payload, "power_on_time", unread, "power_on_hours")
    firmware_version = _reported_text(payload, "firmware_version", unread, "firmware_version")
    nvme_version = _reported_object(payload, "nvme_version", unread, "protocol_version")
    protocol_version = _reported_text(nvme_version, "string", unread, "protocol_version")
    if protocol_version is None and "protocol_version" not in unread:
        sata_version = _reported_object(payload, "sata_version", unread, "protocol_version")
        protocol_version = _reported_text(sata_version, "string", unread, "protocol_version")
    smart_status = _reported_object(payload, "smart_status", unread, "smart_health_status")
    passed = _reported(
        smart_status, "passed", lambda value: value if isinstance(value, bool) else None,
        unread, "smart_health_status",
    )
    smart_health_status = None if passed is None else ("PASSED" if passed else "FAILED")
    rotation_rate = _reported(payload, "rotation_rate", _coerce_non_negative_int, unread, "rotation_rate_rpm")
    form_factor = _reported_text(
        _reported_object(payload, "form_factor", unread, "form_factor"), "name", unread, "form_factor",
    )
    scsi_transport_protocol = _reported_object(payload, "scsi_transport_protocol", unread, "transport_protocol")
    transport_protocol = _reported_text(scsi_transport_protocol, "name", unread, "transport_protocol")
    if transport_protocol is None and "transport_protocol" not in unread:
        device = _reported_object(payload, "device", unread, "transport_protocol")
        transport_protocol = _reported_text(device, "protocol", unread, "transport_protocol")
    logical_unit_id = _reported_sas_address(payload, "logical_unit_id", unread, "logical_unit_id")
    power_on_hours = _reported(power_on_time, "hours", _coerce_non_negative_int, unread, "power_on_hours")
    _derived_unread(unread, "power_on_days", power_on_hours, {"power_on_hours": power_on_hours})
    power_cycle_count = _extract_ata_attribute_raw_value(payload, 12, unread=unread, field="power_cycle_count")
    power_on_resets = _extract_ata_device_stat_int(payload, "Lifetime Power-On Resets", unread, "power_on_resets")
    warning_temperature_c, critical_temperature_c = _extract_smartctl_temperature_thresholds(
        payload, temperature, unread,
    )
    logical_block_size_reported = "logical_block_size" in payload
    logical_block_size = _reported(payload, "logical_block_size", _positive_int, unread, "logical_block_size")
    physical_block_size = _reported(payload, "physical_block_size", _positive_int, unread, "physical_block_size")
    # An absent size keeps the long-standing 512-byte sector default. A reported
    # size that is not a positive integer is rejected, and is never replaced by
    # that default: sector-valued counters then have no byte total.
    sector_size_for_ata = logical_block_size if logical_block_size_reported else 512
    traffic_and_wear = _extract_smartctl_traffic_and_wear(
        payload,
        power_on_hours,
        sector_size_for_ata,
        unread,
    )
    available_spare_percent = traffic_and_wear["available_spare_percent"]
    available_spare_threshold_percent = traffic_and_wear["available_spare_threshold_percent"]
    endurance_used_percent = traffic_and_wear["endurance_used_percent"]
    endurance_remaining_percent = traffic_and_wear["endurance_remaining_percent"]
    bytes_read = traffic_and_wear["bytes_read"]
    bytes_written = traffic_and_wear["bytes_written"]
    annualized_bytes_read = traffic_and_wear["annualized_bytes_read"]
    annualized_bytes_written = traffic_and_wear["annualized_bytes_written"]
    estimated_lifetime_bytes_written = traffic_and_wear["estimated_lifetime_bytes_written"]
    estimated_remaining_bytes_written = traffic_and_wear["estimated_remaining_bytes_written"]
    media_errors = traffic_and_wear["media_errors"]
    unsafe_shutdowns = traffic_and_wear["unsafe_shutdowns"]
    read_commands = _extract_ata_device_stat_int(payload, "Number of Read Commands", unread, "read_commands")
    write_commands = _extract_ata_device_stat_int(payload, "Number of Write Commands", unread, "write_commands")
    hardware_resets = _extract_ata_device_stat_int(payload, "Number of Hardware Resets", unread, "hardware_resets")
    interface_crc_errors = _extract_ata_device_stat_int(
        payload, "Number of Interface CRC Errors", unread, "interface_crc_errors",
    )
    if interface_crc_errors is None and "interface_crc_errors" not in unread:
        interface_crc_errors = _extract_ata_attribute_raw_value(
            payload, 199, unread=unread, field="interface_crc_errors",
        )
    read_cache_enabled = _reported_cache_flag(payload, "read_lookahead", unread, "read_cache_enabled")
    writeback_cache_enabled = _reported_cache_flag(payload, "write_cache", unread, "writeback_cache_enabled")
    interface_speed = _reported_object(payload, "interface_speed", unread, "negotiated_link_rate")
    interface_speed_current = _reported_object(interface_speed, "current", unread, "negotiated_link_rate")
    negotiated_link_rate = _reported_text(interface_speed_current, "string", unread, "negotiated_link_rate")

    # NVMe drives do not rotate; infer 0 only when no rotation rate was
    # reported, never in place of a reported value that could not be read.
    if (
        rotation_rate is None
        and "rotation_rate" not in payload
        and transport_protocol
        and transport_protocol.upper() == "NVME"
    ):
        rotation_rate = 0

    namespace_eui64 = _reported_namespace_eui64(payload, unread)

    phy_unread: set[str] = set()
    sas_phy = _extract_smartctl_sas_phy(payload, phy_unread)
    if sas_phy is None:
        sas_address = None
        attached_sas_address = None
    else:
        # The selected PHY's link rate replaces the interface-speed one.
        unread.discard("negotiated_link_rate")
        unread.update(phy_unread)
        sas_address, attached_sas_address, negotiated_link_rate = sas_phy

    latest_test_type, latest_test_status, latest_test_lifetime_hours = _extract_smartctl_latest_test(payload, unread)
    last_test_age_hours = (
        power_on_hours - latest_test_lifetime_hours
        if isinstance(power_on_hours, int)
        and isinstance(latest_test_lifetime_hours, int)
        and power_on_hours >= latest_test_lifetime_hours
        else None
    )
    _derived_unread(
        unread, "last_test_age_hours", last_test_age_hours,
        {"power_on_hours": power_on_hours, "last_test_lifetime_hours": latest_test_lifetime_hours},
    )

    return _finalize_summary(
        {
            "temperature_c": current_temperature,
            "warning_temperature_c": warning_temperature_c,
            "critical_temperature_c": critical_temperature_c,
            "smart_health_status": smart_health_status,
            "power_cycle_count": power_cycle_count,
            "power_on_resets": power_on_resets,
            "power_on_hours": power_on_hours,
            "power_on_days": power_on_hours // 24 if isinstance(power_on_hours, int) else None,
            "last_test_type": latest_test_type,
            "last_test_status": latest_test_status,
            "last_test_lifetime_hours": latest_test_lifetime_hours,
            "last_test_age_hours": last_test_age_hours,
            "logical_block_size": logical_block_size,
            "physical_block_size": physical_block_size,
            "available_spare_percent": available_spare_percent,
            "available_spare_threshold_percent": available_spare_threshold_percent,
            "endurance_used_percent": endurance_used_percent,
            "endurance_remaining_percent": endurance_remaining_percent,
            "bytes_read": bytes_read,
            "bytes_written": bytes_written,
            "annualized_bytes_read": annualized_bytes_read,
            "annualized_bytes_written": annualized_bytes_written,
            "estimated_lifetime_bytes_written": estimated_lifetime_bytes_written,
            "estimated_remaining_bytes_written": estimated_remaining_bytes_written,
            "read_commands": read_commands,
            "write_commands": write_commands,
            "media_errors": media_errors,
            "unsafe_shutdowns": unsafe_shutdowns,
            "hardware_resets": hardware_resets,
            "interface_crc_errors": interface_crc_errors,
            "rotation_rate_rpm": rotation_rate,
            "form_factor": form_factor,
            "firmware_version": firmware_version,
            "protocol_version": protocol_version,
            "namespace_eui64": namespace_eui64,
            "namespace_nguid": None,
            "read_cache_enabled": read_cache_enabled,
            "writeback_cache_enabled": writeback_cache_enabled,
            "transport_protocol": transport_protocol,
            "logical_unit_id": logical_unit_id,
            "sas_address": sas_address,
            "attached_sas_address": attached_sas_address,
            "negotiated_link_rate": negotiated_link_rate,
            "sas_ports": _extract_smartctl_sas_ports(payload),
            "sata_phy_events": _extract_smartctl_sata_phy_events(payload),
        },
        "No SMART summary fields were returned for this disk.",
        unread=unread,
    )


def parse_nvme_smart_log_summary(output: str) -> dict[str, Any]:
    payload = _load_json_object(output)
    if payload is None:
        return {"available": False, "message": "NVMe smart-log JSON parsing failed."}

    unread: set[str] = set()
    temperature_c = _reported_kelvin(payload, "temperature", unread, "temperature_c")
    power_on_hours = _reported(payload, "power_on_hours", _coerce_non_negative_int, unread, "power_on_hours")
    available_spare_percent = _reported(
        payload, "avail_spare", _coerce_non_negative_int, unread, "available_spare_percent",
    )
    available_spare_threshold_percent = _reported(
        payload, "spare_thresh", _coerce_non_negative_int, unread, "available_spare_threshold_percent",
    )
    endurance_used_percent = _reported(
        payload, "percent_used", _coerce_non_negative_int, unread, "endurance_used_percent",
    )
    endurance_remaining_percent = (
        max(0, 100 - endurance_used_percent) if endurance_used_percent is not None else None
    )
    bytes_read = _reported(payload, "data_units_read", _nvme_data_units_to_bytes, unread, "bytes_read")
    bytes_written = _reported(payload, "data_units_written", _nvme_data_units_to_bytes, unread, "bytes_written")
    media_errors = _reported(payload, "media_errors", _coerce_non_negative_int, unread, "media_errors")
    unsafe_shutdowns = _reported(payload, "unsafe_shutdowns", _coerce_non_negative_int, unread, "unsafe_shutdowns")
    annualized_bytes_read = _annualize_bytes(bytes_read, power_on_hours)
    annualized_bytes_written = _annualize_bytes(bytes_written, power_on_hours)
    estimated_lifetime_bytes_written = (
        int(bytes_written * 100 / endurance_used_percent)
        if bytes_written is not None
        and endurance_used_percent is not None
        and endurance_used_percent > 0
        else None
    )
    estimated_remaining_bytes_written = (
        max(estimated_lifetime_bytes_written - bytes_written, 0)
        if estimated_lifetime_bytes_written is not None and bytes_written is not None
        else None
    )
    _derived_unread(unread, "power_on_days", power_on_hours, {"power_on_hours": power_on_hours})
    _derived_unread(
        unread, "endurance_remaining_percent", endurance_remaining_percent,
        {"endurance_used_percent": endurance_used_percent},
    )
    _derived_unread(
        unread, "annualized_bytes_read", annualized_bytes_read,
        {"bytes_read": bytes_read, "power_on_hours": power_on_hours},
    )
    _derived_unread(
        unread, "annualized_bytes_written", annualized_bytes_written,
        {"bytes_written": bytes_written, "power_on_hours": power_on_hours},
    )
    _derived_unread(
        unread, "estimated_lifetime_bytes_written", estimated_lifetime_bytes_written,
        {"bytes_written": bytes_written, "endurance_used_percent": endurance_used_percent},
    )
    _derived_unread(
        unread, "estimated_remaining_bytes_written", estimated_remaining_bytes_written,
        {"bytes_written": bytes_written, "endurance_used_percent": endurance_used_percent},
    )

    return _finalize_summary(
        {
            "temperature_c": temperature_c,
            "power_on_hours": power_on_hours,
            "power_on_days": power_on_hours // 24 if isinstance(power_on_hours, int) else None,
            "available_spare_percent": available_spare_percent,
            "available_spare_threshold_percent": available_spare_threshold_percent,
            "endurance_used_percent": endurance_used_percent,
            "endurance_remaining_percent": endurance_remaining_percent,
            "bytes_read": bytes_read,
            "bytes_written": bytes_written,
            "annualized_bytes_read": annualized_bytes_read,
            "annualized_bytes_written": annualized_bytes_written,
            "estimated_lifetime_bytes_written": estimated_lifetime_bytes_written,
            "estimated_remaining_bytes_written": estimated_remaining_bytes_written,
            "media_errors": media_errors,
            "unsafe_shutdowns": unsafe_shutdowns,
            "rotation_rate_rpm": 0,
            "transport_protocol": "NVMe",
        },
        "No NVMe smart-log summary fields were returned for this disk.",
        availability_values=(
            temperature_c,
            power_on_hours,
            available_spare_percent,
            available_spare_threshold_percent,
            endurance_used_percent,
            endurance_remaining_percent,
            bytes_read,
            bytes_written,
            annualized_bytes_read,
            annualized_bytes_written,
            estimated_remaining_bytes_written,
            media_errors,
            unsafe_shutdowns,
        ),
        unread=unread,
    )


def parse_nvme_id_ctrl_summary(output: str) -> dict[str, Any]:
    payload = _load_json_object(output)
    if payload is None:
        return {"available": False, "message": "NVMe id-ctrl JSON parsing failed."}

    unread: set[str] = set()
    firmware_version = _reported_text(payload, "fr", unread, "firmware_version")
    protocol_version = _reported(payload, "ver", _format_nvme_version, unread, "protocol_version")
    warning_temperature_c = _reported_kelvin(payload, "wctemp", unread, "warning_temperature_c")
    critical_temperature_c = _reported_kelvin(payload, "cctemp", unread, "critical_temperature_c")

    return _finalize_summary(
        {
            "firmware_version": firmware_version,
            "protocol_version": protocol_version,
            "warning_temperature_c": warning_temperature_c,
            "critical_temperature_c": critical_temperature_c,
        },
        "No NVMe controller identity fields were returned for this disk.",
        unread=unread,
    )


def parse_nvme_id_ns_summary(output: str) -> dict[str, Any]:
    payload = _load_json_object(output)
    if payload is None:
        return {"available": False, "message": "NVMe id-ns JSON parsing failed."}

    unread: set[str] = set()
    return _finalize_summary(
        {
            "namespace_eui64": _reported(payload, "eui64", _format_nvme_eui64, unread, "namespace_eui64"),
            "namespace_nguid": _reported(payload, "nguid", _format_nvme_nguid, unread, "namespace_nguid"),
        },
        "No NVMe namespace identity fields were returned for this disk.",
        unread=unread,
    )


_SATA_CURRENT_LINK_RATE_PATTERN = re.compile(r"\(current:\s*([^)]+)\)", re.IGNORECASE)


def parse_smartctl_text_enrichment(output: str) -> dict[str, Any]:
    read_cache_enabled: bool | None = None
    writeback_cache_enabled: bool | None = None
    smart_health_status: str | None = None
    protocol_version: str | None = None
    transport_protocol: str | None = None
    logical_unit_id: str | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    negotiated_link_rate: str | None = None
    trim_supported: bool | None = None
    sas_ports = _extract_smartctl_sas_ports_text(output)
    sata_phy_events = _extract_smartctl_sata_phy_text(output)

    for raw_line in output.splitlines():
        line = raw_line.strip()
        cache_toggle = next(
            (
                (target, _parse_enabled_disabled(line.split(":", 1)[1]))
                for prefix, target in SMARTCTL_CACHE_TOGGLE_PREFIXES.items()
                if line.startswith(prefix)
            ),
            None,
        )
        if cache_toggle is not None:
            target, enabled = cache_toggle
            if enabled is None:
                continue
            if target == "read":
                read_cache_enabled = enabled
            else:
                writeback_cache_enabled = enabled
        elif line.startswith("SMART Health Status:"):
            smart_health_status = normalize_text(line.split(":", 1)[1])
        elif line.startswith("SMART overall-health self-assessment test result:"):
            smart_health_status = normalize_text(line.split(":", 1)[1])
        elif line.startswith("SATA Version is:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                # The match ends at a ")", so the search stops at the last
                # one: with no ")" after it, a whitespace run after "current:"
                # was retried from every position (#1216).
                current_match = _SATA_CURRENT_LINK_RATE_PATTERN.search(value, 0, value.rfind(")") + 1)
                if current_match:
                    negotiated_link_rate = normalize_text(current_match.group(1))
                    value = normalize_text(value[: current_match.start()].rstrip(" ,")) or value
                protocol_version = value
        elif line.startswith("Transport protocol:"):
            transport_protocol = normalize_text(line.split(":", 1)[1])
        elif line.startswith("Logical Unit id:"):
            logical_unit_id = format_hex_identifier(line.split(":", 1)[1])
        elif line.startswith("SAS address ="):
            sas_address = format_hex_identifier(line.split("=", 1)[1])
        elif line.startswith("attached SAS address ="):
            attached_sas_address = format_hex_identifier(line.split("=", 1)[1])
        elif line.startswith("negotiated logical link rate:"):
            negotiated_link_rate = normalize_text(line.split(":", 1)[1])
        elif line.startswith("TRIM Command:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                lowered = value.lower()
                if lowered.startswith("available"):
                    trim_supported = True
                elif lowered.startswith("unavailable") or lowered.startswith("not supported"):
                    trim_supported = False

    return {
        "available": any(
            value is not None
            for value in (
                read_cache_enabled,
                writeback_cache_enabled,
                smart_health_status,
                protocol_version,
                transport_protocol,
                logical_unit_id,
                sas_address,
                attached_sas_address,
                negotiated_link_rate,
                trim_supported,
                sas_ports,
                sata_phy_events,
            )
        ),
        "read_cache_enabled": read_cache_enabled,
        "writeback_cache_enabled": writeback_cache_enabled,
        "smart_health_status": smart_health_status,
        "protocol_version": protocol_version,
        "transport_protocol": transport_protocol,
        "logical_unit_id": logical_unit_id,
        "sas_address": sas_address,
        "attached_sas_address": attached_sas_address,
        "negotiated_link_rate": negotiated_link_rate,
        "trim_supported": trim_supported,
        "sas_ports": sas_ports,
        "sata_phy_events": sata_phy_events,
    }
