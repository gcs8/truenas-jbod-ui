from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS_PATH = REPOSITORY_ROOT / "tests" / "public_text_privacy_exceptions.json"


def load_reviewed_exceptions(
    path: Path,
) -> dict[str, tuple[str, dict[str, dict[str, int]]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("reviewed privacy exceptions must be a JSON object")

    reviewed: dict[str, tuple[str, dict[str, dict[str, int]]]] = {}
    for relative_path, entry in payload.items():
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("reviewed privacy exception paths must be non-empty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"reviewed privacy exception for {relative_path} must be an object")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"reviewed privacy exception for {relative_path} needs a non-empty reason")
        findings = entry.get("findings")
        if not isinstance(findings, dict) or not findings:
            raise ValueError(f"reviewed privacy exception for {relative_path} needs findings")
        for category, fingerprints in findings.items():
            if not isinstance(category, str) or not category or not isinstance(fingerprints, dict) or not fingerprints:
                raise ValueError(f"reviewed privacy exception for {relative_path} has invalid findings")
            for fingerprint, count in fingerprints.items():
                if (
                    not isinstance(fingerprint, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
                    or isinstance(count, bool)
                    or not isinstance(count, int)
                    or count <= 0
                ):
                    raise ValueError(f"reviewed privacy exception for {relative_path} has invalid findings")
        reviewed[relative_path] = (reason, findings)
    return reviewed


REVIEWED_FINDING_EXCEPTIONS = load_reviewed_exceptions(EXCEPTIONS_PATH)

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
HOST_FIELD = re.compile(r"(?i)\bhost(?:\s*name)?\s*:\s*`([^`]+)`")
YAML_HOST_FIELD = re.compile(r"(?i)^\s*host\s*:\s*([^\s#]+)")
INLINE_SERIAL = re.compile(r"(?i)\bserial(?: number| family)?\s*:\s*`([^`]+)`")
SERIAL_LIST_START = re.compile(r"^(\s*)serials:\s*$")
SERIAL_LIST_VALUE = re.compile(r"^\s*-\s*([^\s#]+)")


def discover_tracked_public_text(root: Path = REPOSITORY_ROOT) -> tuple[str, ...]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    attributes = subprocess.run(
        ["git", "check-attr", "-z", "--stdin", "text"],
        cwd=root,
        input=tracked,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")

    paths: list[str] = []
    for index in range(0, len(attributes) - 1, 3):
        raw_path, attribute, value = attributes[index : index + 3]
        if attribute != b"text":
            raise RuntimeError(f"unexpected Git attribute response for {raw_path!r}")
        if value == b"unset":
            continue
        if value not in {b"auto", b"set"}:
            raise RuntimeError(f"tracked path has no explicit text policy: {raw_path!r}")
        relative_path = raw_path.decode("utf-8")
        text = (root / relative_path).read_text(encoding="utf-8")
        if "\0" in text:
            raise ValueError(f"tracked text path contains a NUL byte: {relative_path}")
        paths.append(relative_path)
    return tuple(paths)


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


def _finding_fingerprint(category: str, values: Counter[str]) -> str:
    serialized = json.dumps(
        sorted(values.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{category}\0{serialized}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _scan_text(text: str) -> Counter[tuple[str, str]]:
    values_by_category: dict[str, Counter[str]] = {}

    def add(category: str, value: str) -> None:
        values_by_category.setdefault(category, Counter())[value] += 1

    for match in IPV4_CANDIDATE.finditer(text):
        if _is_rfc1918(match.group()):
            add("rfc1918_ipv4", match.group())
    for category, pattern in (
        ("private_posix_host_path", PRIVATE_POSIX_PATH),
        ("local_windows_drive_path", LOCAL_WINDOWS_PATH),
        ("internal_hostname", INTERNAL_HOST_SUFFIX),
        ("compact_lab_host_id", COMPACT_LAB_HOST_ID),
        ("sas_wwn_identifier", SAS_WWN_IDENTIFIER),
    ):
        for match in pattern.finditer(text):
            add(category, match.group())

    serial_list_indent: int | None = None
    for line in text.splitlines():
        host_matches = [match.group(1) for match in HOST_FIELD.finditer(line)]
        yaml_host = YAML_HOST_FIELD.match(line)
        if yaml_host:
            host_matches.append(yaml_host.group(1))
        for value in host_matches:
            if not _is_safe_documentation_host(value):
                add("internal_hostname", value)

        for match in INLINE_SERIAL.finditer(line):
            value = match.group(1)
            if not value.upper().startswith("SANITIZED-"):
                add("realistic_serial_example", value)

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
            add("realistic_serial_example", serial_value.group(1))
    return Counter(
        {
            (category, _finding_fingerprint(category, values)): sum(values.values())
            for category, values in values_by_category.items()
        }
    )


def scan_tracked_public_text(
    root: Path = REPOSITORY_ROOT,
) -> Counter[tuple[str, str, str]]:
    findings: Counter[tuple[str, str, str]] = Counter()
    for relative_path in discover_tracked_public_text(root):
        text = (root / relative_path).read_text(encoding="utf-8")
        for (category, fingerprint), count in _scan_text(text).items():
            findings[(relative_path, category, fingerprint)] = count
    return findings


class PublicDocPrivacyTests(unittest.TestCase):
    def test_tracked_public_text_discovery_covers_repository_surfaces(self) -> None:
        paths = set(discover_tracked_public_text())

        self.assertTrue(
            {
                ".env.example",
                "README.md",
                "config/config.example.yaml",
                "public-demo/index.html",
                "qa/public-demo.spec.js",
                "scripts/build_public_demo.py",
                "tests/fixtures/platform_parity/linux_lsblk.json",
                "wiki/Home.md",
            }.issubset(paths)
        )
        self.assertNotIn("docs/images/ui-overview.png", paths)

    def test_new_tracked_public_text_is_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitattributes").write_text(
                "* text=auto eol=lf\n*.png binary\n",
                encoding="utf-8",
            )
            (root / "safe.txt").write_text("public text\n", encoding="utf-8")
            (root / "new-script.py").write_text(
                'TARGET = "10.23.45.67"\n',
                encoding="utf-8",
            )
            (root / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
            subprocess.run(["git", "add", "."], cwd=root, check=True)

            findings = scan_tracked_public_text(root)

        self.assertEqual(
            sum(
                count
                for (path, category, _fingerprint), count in findings.items()
                if path == "new-script.py" and category == "rfc1918_ipv4"
            ),
            1,
        )
        self.assertFalse(any(path == "asset.png" for path, _category, _fingerprint in findings))

    def test_reviewed_exception_config_fails_closed_without_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "exceptions.json"
            path.write_text(
                json.dumps(
                    {
                        "example.txt": {
                            "reason": "",
                            "findings": {"rfc1918_ipv4": 1},
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "non-empty reason"):
                load_reviewed_exceptions(path)

    def test_privacy_findings_pin_values_instead_of_only_category_counts(self) -> None:
        first = _scan_text("host: 10.23.45.67\n")
        replacement = _scan_text("host: 10.23.45.68\n")

        self.assertNotEqual(first, replacement)

    def test_tracked_public_text_contains_no_unreviewed_private_lab_identifiers(self) -> None:
        expected: Counter[tuple[str, str, str]] = Counter()
        for path, (reason, categories) in REVIEWED_FINDING_EXCEPTIONS.items():
            self.assertTrue(reason.strip(), path)
            for category, fingerprints in categories.items():
                for fingerprint, count in fingerprints.items():
                    expected[(path, category, fingerprint)] = count

        findings = scan_tracked_public_text()

        self.assertEqual(
            findings,
            expected,
            msg=(
                "tracked public text privacy findings differ from reviewed exceptions: "
                f"{json.dumps({f'{path}:{category}:{fingerprint}': count for (path, category, fingerprint), count in findings.items()}, sort_keys=True)}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
