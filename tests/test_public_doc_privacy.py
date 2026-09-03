from __future__ import annotations

import ipaddress
import json
import re
import unittest
from collections import Counter
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = (
    "docs/HANDOFF_0.21.0_RELEASE_20260612.md",
    "docs/V0_3_SCALE_NOTES.md",
    "docs/ESXI_PLATFORM_FEASIBILITY.md",
    "docs/GPU_SERVER_NOTES.md",
    "docs/QUANTASTOR_NOTES.md",
    "wiki/Quantastor-Setup.md",
)

IPV4_CANDIDATE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
PRIVATE_POSIX_PATH = re.compile(r"(?<![\w<])/(?:home|Users|mnt|media|Volumes)/[^\s`\"']+")
LOCAL_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:[\\/])[^\s`\"']+")
INTERNAL_HOST_SUFFIX = re.compile(
    r"(?i)\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.(?:local|lan|internal)\b"
)
COMPACT_LAB_HOST_ID = re.compile(r"(?i)(?<![a-z0-9])[hn][0-9a-f]{4}(?![a-z0-9])")
SAS_WWN_IDENTIFIER = re.compile(r"(?i)(?<![a-z0-9])(?:0x)?[0-9a-f]{16}(?![a-z0-9])")
HOST_FIELD = re.compile(r"(?i)\b[^`\n:]*host(?:\s*name)?\s*:\s*`([^`]+)`")
YAML_HOST_FIELD = re.compile(r"(?i)^\s*host\s*:\s*([^\s#]+)")
INLINE_SERIAL = re.compile(r"(?i)\bserial(?: number| family)?\s*:\s*`([^`]+)`")
SERIAL_LIST_START = re.compile(r"^(\s*)serials:\s*$")
SERIAL_LIST_VALUE = re.compile(r"^\s*-\s*([^\s#]+)")


def _is_rfc1918(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in RFC1918_NETWORKS)


def _is_safe_documentation_host(value: str) -> bool:
    host = value.strip().strip("`'\"").strip("[]").rstrip(".").lower()
    if not host or host.startswith("<") or host in {"localhost", "127.0.0.1"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".example.test")
    return not any(address in network for network in RFC1918_NETWORKS)


def scan_public_docs() -> Counter[str]:
    findings: Counter[str] = Counter()

    for relative_path in PUBLIC_DOCS:
        text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")

        findings["rfc1918_ipv4"] += sum(_is_rfc1918(match.group()) for match in IPV4_CANDIDATE.finditer(text))
        findings["private_posix_host_path"] += len(PRIVATE_POSIX_PATH.findall(text))
        findings["local_windows_drive_path"] += len(LOCAL_WINDOWS_PATH.findall(text))
        findings["internal_hostname"] += len(INTERNAL_HOST_SUFFIX.findall(text))
        findings["compact_lab_host_id"] += len(COMPACT_LAB_HOST_ID.findall(text))
        findings["sas_wwn_identifier"] += len(SAS_WWN_IDENTIFIER.findall(text))

        serial_list_indent: int | None = None
        for line in text.splitlines():
            host_matches = [match.group(1) for match in HOST_FIELD.finditer(line)]
            yaml_host = YAML_HOST_FIELD.match(line)
            if yaml_host:
                host_matches.append(yaml_host.group(1))
            findings["internal_hostname"] += sum(
                not _is_safe_documentation_host(value) for value in host_matches
            )

            findings["realistic_serial_example"] += sum(
                not match.group(1).upper().startswith("SANITIZED-") for match in INLINE_SERIAL.finditer(line)
            )

            serial_list = SERIAL_LIST_START.match(line)
            if serial_list:
                serial_list_indent = len(serial_list.group(1))
                continue
            if serial_list_indent is None:
                continue
            if line.strip() and len(line) - len(line.lstrip()) <= serial_list_indent:
                serial_list_indent = None
                continue
            serial_value = SERIAL_LIST_VALUE.match(line)
            if serial_value and not serial_value.group(1).upper().startswith("SANITIZED-"):
                findings["realistic_serial_example"] += 1

    return findings


class PublicDocPrivacyTests(unittest.TestCase):
    def test_public_doc_allowlist_contains_no_private_lab_identifiers(self) -> None:
        self.assertEqual(
            set(PUBLIC_DOCS),
            {
                "docs/HANDOFF_0.21.0_RELEASE_20260612.md",
                "docs/V0_3_SCALE_NOTES.md",
                "docs/ESXI_PLATFORM_FEASIBILITY.md",
                "docs/GPU_SERVER_NOTES.md",
                "docs/QUANTASTOR_NOTES.md",
                "wiki/Quantastor-Setup.md",
            },
        )
        self.assertTrue(all((REPOSITORY_ROOT / path).is_file() for path in PUBLIC_DOCS))

        counts = scan_public_docs()

        self.assertEqual(
            sum(counts.values()),
            0,
            msg=f"public documentation privacy findings by category: {json.dumps(counts, sort_keys=True)}",
        )


if __name__ == "__main__":
    unittest.main()
