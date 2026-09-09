from __future__ import annotations

import argparse
import gzip
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.public_demo_source_parity import (  # noqa: E402
    check_source_parity_manifest,
    parse_manifest,
    recorded_source_revision_errors,
)


DEFAULT_DEMO_DIR = Path("public-demo")
DEFAULT_MAX_RAW_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_GZIP_BYTES = 1_835_008
PRIVATE_IPV4_PATTERN = re.compile(
    r"(?<![0-9])(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))"
    r"\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(?![0-9])"
)
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private IPv4 address", PRIVATE_IPV4_PATTERN),
    (
        "non-empty credential value",
        re.compile(r'(?i)"(?:api_key|api_password|password|secret|token)"\s*:\s*"(?!")'),
    ),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenSSH key material", re.compile(r"\bOPENSSH PRIVATE KEY\b")),
    ("non-demo serial", re.compile(r"(?i)\bserial(?:_number)?[\"']?\s*[:=]\s*[\"'](?!DEMO-|null)[^\"']+")),
)
REQUIRED_MARKERS: tuple[str, ...] = (
    "Demo data",
    'id="snapshot-app-version">v',
    "Captured",
    "Synthetic IDs",
    "Source revision",
    "Build ID",
    "Demo 60-Bay Top Loader",
    "Demo 4x NVMe Carrier",
    "Demo Boot Modules",
    "mirror-8",
)
FORBIDDEN_MARKERS: tuple[tuple[str, str], ...] = (
    ("snapshot Storage Fabric route action", 'id="sas-fabric-view-link"'),
    ("live-derived provenance claim", "Live-derived"),
    ("local history dependency", "history/history.db"),
)
ARTIFACT_VERSION_PATTERN = re.compile(r'id="snapshot-app-version">v(?P<version>[0-9A-Za-z][0-9A-Za-z.+-]*)<')
RESOURCE_REFERENCE_PATTERN = re.compile(
    r"<(?:script|img|link|source|video|audio|iframe)\b[^>]*\b(?:src|href|poster)\s*=\s*[\"'](?!data:|#)[^\"']+",
    re.IGNORECASE,
)
SOURCE_VERSION_PATTERN = re.compile(
    r'^__version__\s*=\s*["\'](?P<version>[0-9A-Za-z][0-9A-Za-z.+-]*)["\']\s*$',
    re.MULTILINE,
)


def read_source_version(source_root: Path) -> str:
    version_path = source_root / "app" / "__init__.py"
    try:
        source = version_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"unable to read source version: {version_path}") from exc
    match = SOURCE_VERSION_PATTERN.search(source)
    if match is None:
        raise ValueError(f"unable to parse source version: {version_path}")
    return match.group("version")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check that the deterministic checked-in public demo is publishable.")
    parser.add_argument("demo_dir", nargs="?", type=Path, default=DEFAULT_DEMO_DIR)
    parser.add_argument("--max-raw-bytes", type=int, default=DEFAULT_MAX_RAW_BYTES)
    parser.add_argument("--max-gzip-bytes", type=int, default=DEFAULT_MAX_GZIP_BYTES)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=ROOT,
        help="Repository root containing the centrally declared source graph.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    index_path = args.demo_dir / "index.html"
    nojekyll_path = args.demo_dir / ".nojekyll"
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
        errors.append(f"public demo artifact raw size {raw_size} exceeds budget {args.max_raw_bytes}")
    if args.max_gzip_bytes > 0 and gzip_size > args.max_gzip_bytes:
        errors.append(f"public demo artifact gzip size {gzip_size} exceeds budget {args.max_gzip_bytes}")

    try:
        html = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        errors.append("public demo artifact is not valid UTF-8")
        html = ""
    if html:
        errors.extend(check_source_parity_manifest(html, source_root=args.source_root))
        manifest, _artifact_html, manifest_errors = parse_manifest(html)
        if not manifest_errors:
            source_revision = manifest.get("source_revision")
            if isinstance(source_revision, str):
                errors.extend(
                    recorded_source_revision_errors(
                        source_root=args.source_root,
                        source_revision=source_revision,
                    )
                )
        try:
            source_version = read_source_version(args.source_root)
        except ValueError as exc:
            errors.append(str(exc))
            source_version = None
        for marker in REQUIRED_MARKERS:
            if marker not in html:
                errors.append(f"missing required marker: {marker}")
        versions = {match.group("version") for match in ARTIFACT_VERSION_PATTERN.finditer(html)}
        if not versions:
            errors.append("missing parseable artifact app version")
        for artifact_version in sorted(versions):
            if source_version is not None and artifact_version != source_version:
                errors.append(f"artifact app version {artifact_version} does not match source {source_version}")
        for label, marker in FORBIDDEN_MARKERS:
            if marker in html:
                errors.append(f"found forbidden {label}")
        if RESOURCE_REFERENCE_PATTERN.search(html):
            errors.append("found external or local resource reference")
        for label, pattern in SENSITIVE_PATTERNS:
            if match := pattern.search(html):
                errors.append(f"found {label}: {match.group(0)[:80]}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Public demo artifact is publishable: {index_path} (raw={raw_size} bytes, gzip={gzip_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
