from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


DEFAULT_DEMO_DIR = Path("public-demo")

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
    "Frozen Offline Artifact",
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

    html = index_path.read_text(encoding="utf-8")
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

    print(f"Public demo artifact is publishable: {index_path} ({index_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
