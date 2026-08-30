from __future__ import annotations

import argparse
import gzip
from pathlib import Path
import re
import sys


DEFAULT_DEMO_DIR = Path("public-demo")
DEFAULT_MAX_RAW_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_GZIP_BYTES = 1_835_008

PRIVATE_IPV4_PATTERN = re.compile(
    r"(?<![0-9])"
    r"(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))"
    r"\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"(?![0-9])"
)

SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private IPv4 address", PRIVATE_IPV4_PATTERN),
    ("API key environment name", re.compile(r"\b(?:TRUENAS_API_KEY|API_KEY|SECRET_KEY)\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenSSH key material", re.compile(r"\bOPENSSH PRIVATE KEY\b")),
    ("known live disk serial", re.compile(r"\b(?:S464NB0K900412E|PHKM8522005N200E|SMC0515D93717D7B1810)\b")),
    ("known live SAS/NAA identifier", re.compile(r"\b500304801f(?:5a00bf|715f3f|5a003f)\b", re.IGNORECASE)),
)

REQUIRED_MARKERS: tuple[str, ...] = (
    "Frozen Sanitized Snapshot",
    "Artifact app v",
    "Capture time",
    'id="sas-fabric-view-link" href="#sas-fabric-panel"',
    'sasFabricViewUrl: "#sas-fabric-panel"',
    "Live-derived CORE 60-bay sample",
    "Scrambled IDs",
    "4x NVMe Carrier Card",
    "Boot SATADOMs",
    "mirror-8",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that the checked-in public demo artifact is publishable.",
    )
    parser.add_argument(
        "demo_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_DEMO_DIR,
        help="Directory containing the static public demo files.",
    )
    parser.add_argument(
        "--max-raw-bytes",
        type=int,
        default=DEFAULT_MAX_RAW_BYTES,
        help=(
            "Maximum allowed raw index.html size in bytes. "
            f"Defaults to {DEFAULT_MAX_RAW_BYTES}; pass 0 to disable."
        ),
    )
    parser.add_argument(
        "--max-gzip-bytes",
        type=int,
        default=DEFAULT_MAX_GZIP_BYTES,
        help=(
            "Maximum allowed gzip-9 index.html size in bytes. "
            f"Defaults to {DEFAULT_MAX_GZIP_BYTES}; pass 0 to disable."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    demo_dir = args.demo_dir
    index_path = demo_dir / "index.html"
    nojekyll_path = demo_dir / ".nojekyll"

    errors: list[str] = []
    if not index_path.exists():
        errors.append(f"missing {index_path}")
    if not nojekyll_path.exists():
        errors.append(f"missing {nojekyll_path}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    raw_bytes = index_path.read_bytes()
    raw_size = len(raw_bytes)
    gzip_size = len(gzip.compress(raw_bytes, compresslevel=9, mtime=0))

    if args.max_raw_bytes > 0 and raw_size > args.max_raw_bytes:
        errors.append(
            f"public demo artifact raw size {raw_size} exceeds budget {args.max_raw_bytes}"
        )
    if args.max_gzip_bytes > 0 and gzip_size > args.max_gzip_bytes:
        errors.append(
            f"public demo artifact gzip size {gzip_size} exceeds budget {args.max_gzip_bytes}"
        )

    html = raw_bytes.decode("utf-8")
    for marker in REQUIRED_MARKERS:
        if marker not in html:
            errors.append(f"missing required marker: {marker}")

    for label, pattern in SENSITIVE_PATTERNS:
        match = pattern.search(html)
        if match:
            excerpt = match.group(0)
            errors.append(f"found {label}: {excerpt}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(
        "Public demo artifact is publishable: "
        f"{index_path} (raw={raw_size} bytes, gzip={gzip_size} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
