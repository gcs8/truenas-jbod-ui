"""Bounded typed decoding of the Linux SAS transport class survey."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.services.core_expander_counters import SYSFS_COUNTER_ATTRIBUTES

MAX_TRANSPORT_BYTES = 2 * 1024 * 1024
MAX_TRANSPORT_ROWS = 4096
KINDS = {"sas_host", "scsi_host", "sas_expander", "sas_port", "sas_phy", "sas_device", "sas_end_device"}
NAME_PATTERNS = {
    "sas_host": r"host[0-9]+",
    "scsi_host": r"host[0-9]+",
    "sas_expander": r"expander-[0-9]+:[0-9]+",
    "sas_port": r"port-[0-9]+(?::[0-9]+){1,2}",
    "sas_phy": r"phy-[0-9]+(?::[0-9]+){1,2}",
    "sas_device": r"(?:expander|end_device)-[0-9]+(?::[0-9]+){1,2}",
    "sas_end_device": r"end_device-[0-9]+(?::[0-9]+){1,2}",
}


@dataclass(frozen=True)
class SasTransportRow:
    kind: str
    name: str
    path: str
    attrs: dict[str, str | None]
    links: dict[str, str]
    sas_address: str | None = None
    enclosure_identifier: str | None = None
    bay_identifier: int | None = None
    phy: int | None = None
    num_phys: int | None = None
    negotiated_rate_gbps: float | None = None
    maximum_rate_gbps: float | None = None
    counters: dict[str, int | None] = field(default_factory=dict)
    not_read: tuple[str, ...] = ()
    minimum_rate_gbps: float | None = None

    @property
    def owner_path(self) -> str:
        return self.path.rsplit("/", 1)[0]


@dataclass(frozen=True)
class SasTransportData:
    rows: tuple[SasTransportRow, ...] = ()
    complete: bool = False
    reason: str | None = "not_read"

    def of_kind(self, kind: str) -> list[SasTransportRow]:
        return [row for row in self.rows if row.kind == kind]


def transport_matches_selected_blocks(data: SasTransportData, blocks: list[str]) -> bool:
    """Admit a no-SES view only when all its disk routes can be retained.

    A host-wide HBA census is not evidence for the selected view. Require
    unique primary block bindings and an explicit nested port chain to a host;
    mixed or unjoined views keep their existing platform-storage routes.
    """
    if not data.complete or not blocks or any(not block for block in blocks) or len(set(blocks)) != len(blocks):
        return False
    admitted = {row.path for row in data.of_kind("sas_host")}
    ports = {row.path: row for row in data.of_kind("sas_port")}

    def attached(row: SasTransportRow) -> bool:
        port = ports.get(row.owner_path)
        return port is not None and port.owner_path in admitted and port.links.get(row.name) == row.path

    # Parents precede nested children. Only controllers and expanders can own
    # downstream ports, matching the projection's ancestry admission.
    for row in sorted(data.of_kind("sas_expander"), key=lambda row: len(row.path)):
        if attached(row):
            admitted.add(row.path)
    ends = data.of_kind("sas_end_device")
    claims = Counter(block for row in ends for block in row.links)
    matched = {block for row in ends if attached(row) for block in row.links}
    return all(claims[block] == 1 and block in matched for block in blocks)


def _integer(value: str | None, maximum: int) -> int | None:
    if value is None or re.fullmatch(r"[0-9]{1,10}", value) is None:
        return None
    number = int(value)
    return number if number <= maximum else None


def _address(value: str | None) -> str | None:
    if value is None or re.fullmatch(r"(?:0x)?[0-9a-fA-F]{16}", value) is None:
        return None
    value = value.lower().removeprefix("0x")
    return value if int(value, 16) else None


def _rate(value: str | None) -> float | None:
    if value is None or re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,3})? Gbit", value) is None:
        return None
    result = float(value.split()[0])
    return result if 0 < result <= 100 else None


def _path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 4096
        and value.startswith("/sys/devices/")
        and re.fullmatch(r"/[A-Za-z0-9_./:+-]+", value) is not None
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
    )


def _row(value: Any) -> SasTransportRow:
    if not isinstance(value, dict) or set(value) != {"kind", "name", "path", "attrs", "links"}:
        raise ValueError("row_shape")
    kind, name, path = (value[key] for key in ("kind", "name", "path"))
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("row_kind")
    if not isinstance(name, str) or len(name) > 128 or re.fullmatch(NAME_PATTERNS[kind], name) is None:
        raise ValueError("row_name")
    if not _path(path) or path.rsplit("/", 1)[-1] != name:
        raise ValueError("row_path")
    attrs, links = value["attrs"], value["links"]
    if not isinstance(attrs, dict) or len(attrs) > 32 or not isinstance(links, dict) or len(links) > 2048:
        raise ValueError("row_fields")
    if any(
        not isinstance(key, str)
        or re.fullmatch(r"[a-z_]{1,64}", key) is None
        or (item is not None and (not isinstance(item, str) or len(item) > 512))
        for key, item in attrs.items()
    ):
        raise ValueError("row_attributes")
    # A bad firmware string is one unread field, not a failed topology census.
    # Preserve structural/type refusal above and do not mutate the source row.
    attrs = {
        key: None if isinstance(item, str) and any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in item) else item
        for key, item in attrs.items()
    }
    if any(
        not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}", key) is None or not _path(item)
        for key, item in links.items()
    ):
        raise ValueError("row_links")
    numbers = {
        key: _integer(attrs.get(key), maximum)
        for key, maximum in (("phy_identifier", 255), ("bay_identifier", 65535), ("num_phys", 256))
    }
    counters = (
        {target: _integer(attrs.get(source), 0xFFFFFFFF) for source, target in SYSFS_COUNTER_ATTRIBUTES.items()}
        if kind == "sas_phy"
        else {}
    )
    rate = _rate(attrs.get("negotiated_linkrate"))
    maximum = _rate(attrs.get("maximum_linkrate"))
    not_read = [key for key, item in attrs.items() if item is None or item.lower() in {"unknown", "invalid argument"}]
    not_read.extend(key for key, item in numbers.items() if key in attrs and item is None)
    not_read.extend(key for key, item in counters.items() if item is None)
    if kind == "sas_phy":
        not_read.extend(
            key for key, item in (("negotiated_linkrate", rate), ("maximum_linkrate", maximum)) if item is None
        )
    return SasTransportRow(
        kind,
        name,
        path,
        attrs,
        links,
        _address(attrs.get("sas_address") or attrs.get("host_sas_address")),
        _address(attrs.get("enclosure_identifier")),
        numbers["bay_identifier"],
        numbers["phy_identifier"],
        numbers["num_phys"],
        rate,
        maximum,
        counters,
        tuple(sorted(set(not_read))),
        _rate(attrs.get("minimum_linkrate")),
    )


def parse_sas_transport(output: str) -> SasTransportData:
    """Reject incomplete/malformed censuses as a whole; field reads stay nullable."""
    if not output:
        return SasTransportData()
    if len(output) > MAX_TRANSPORT_BYTES or len(output.encode("utf-8")) > MAX_TRANSPORT_BYTES:
        return SasTransportData(reason="byte_budget")
    try:
        payload = json.loads(output)
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("version")
        if payload.get("complete") is not True:
            return SasTransportData(reason="incomplete_survey")
        rows = payload.get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_TRANSPORT_ROWS:
            raise ValueError("row_budget")
        decoded = tuple(_row(row) for row in rows)
        identities = [(row.kind, row.name) for row in decoded]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate_identity")
        return SasTransportData(decoded, True, None)
    except (ValueError, TypeError, RecursionError):
        return SasTransportData(reason="invalid_survey")
