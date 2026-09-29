from __future__ import annotations

import argparse
import gzip
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.public_demo_source_parity import (  # noqa: E402
    check_source_parity_manifest,
    parse_manifest,
    recorded_source_integrity,
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


BOOTSTRAP_ASSIGNMENT = re.compile(r"\bwindow\.APP_BOOTSTRAP\s*=")
BOOTSTRAP_SCRIPT = re.compile(r"\s*window\.APP_BOOTSTRAP\s*=\s*(\{.*\})\s*;\s*", re.DOTALL)
# Preserve JSON strings as whole tokens before quoting the template's bare
# property names or removing its trailing commas. Never evaluate JavaScript.
BOOTSTRAP_TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|(?P<key>[A-Za-z_$][\w$]*)(?=\s*:)|(?P<trailing>,)\s*(?=[}\]])'
)
SERIAL_TEXT_PATTERN = re.compile(r"(?i)\bserial(?:_number)?[\"']?\s*[:=]\s*[\"'](?!DEMO-|null)[^\"']+")


class ArtifactScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def serial_data_errors(value: object, path: str) -> list[str]:
    if isinstance(value, dict):
        errors = []
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key.lower() in {"serial", "serial_number"} and item not in (None, "", "null"):
                if not isinstance(item, str) or not item.upper().startswith("DEMO-"):
                    errors.append(f"found non-demo serial at {child_path}")
            errors.extend(serial_data_errors(item, child_path))
        return errors
    if isinstance(value, list):
        return [error for i, item in enumerate(value) for error in serial_data_errors(item, f"{path}[{i}]")]
    if isinstance(value, str):
        # History details can themselves contain serialized JSON; raw report
        # strings can also contain the legacy serial: '...' representation.
        try:
            nested = json.loads(value, object_pairs_hook=unique_json_object)
        except json.JSONDecodeError:
            # Only non-JSON text gets the raw-report fallback. Duplicate keys
            # and depth failures must reach the fail-closed bootstrap handler.
            nested = None
        if isinstance(nested, (dict, list)):
            return serial_data_errors(nested, path)
        if SERIAL_TEXT_PATTERN.search(value):
            return [f"found non-demo serial at {path}"]
    return []


def bootstrap_serial_errors(html: str) -> list[str]:
    """Validate exported data, not UI labels in the fingerprinted app source.

    The template serializes every snapshot, storage view, SMART/history cache,
    fabric and metadata payload into APP_BOOTSTRAP. Inspect the entire object,
    including new properties, rather than maintaining a partial field allowlist.
    Source/output fingerprints and all other sensitive scans still cover the
    entire HTML in both recorded-source and current-source modes.
    """
    parser = ArtifactScripts()
    parser.feed(html)
    scripts = [script for script in parser.scripts if BOOTSTRAP_ASSIGNMENT.search(script)]
    if len(scripts) != 1:
        return ["missing or ambiguous public demo APP_BOOTSTRAP data"]
    match = BOOTSTRAP_SCRIPT.fullmatch(scripts[0])
    if match is None:
        return ["invalid public demo APP_BOOTSTRAP data"]

    def json_token(token: re.Match[str]) -> str:
        if token.group("key") is not None:
            return json.dumps(token.group("key"))
        return "" if token.group("trailing") is not None else token.group(0)

    try:
        payload = json.loads(BOOTSTRAP_TOKEN.sub(json_token, match.group(1)), object_pairs_hook=unique_json_object)
        return serial_data_errors(payload, "APP_BOOTSTRAP")
    except (ValueError, RecursionError):
        return ["invalid public demo APP_BOOTSTRAP data"]


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
    parser.add_argument(
        "demo_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_DEMO_DIR,
        help="Directory holding the built public demo (default: %(default)s).",
    )
    parser.add_argument(
        "--max-raw-bytes",
        type=int,
        default=DEFAULT_MAX_RAW_BYTES,
        help="Fail if the uncompressed page exceeds this many bytes (default: %(default)s).",
    )
    parser.add_argument(
        "--max-gzip-bytes",
        type=int,
        default=DEFAULT_MAX_GZIP_BYTES,
        help="Fail if the gzipped page exceeds this many bytes (default: %(default)s).",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=ROOT,
        help="Repository root containing the centrally declared source graph.",
    )
    parser.add_argument(
        "--require-current",
        action="store_true",
        help=(
            "Release check: also require the artifact to be built from the current source, "
            "with no declared demo input changed since its recorded source revision. "
            "Without this flag the artifact only has to match the commit it records."
        ),
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
        recorded = None if args.require_current else recorded_source_integrity(html, source_root=args.source_root)
        if recorded is not None:
            # Pull-request mode: the artifact must be an exact build of the
            # reachable commit it records. Later source changes are expected;
            # the demo is rebuilt when a release is cut.
            recorded_errors, source_version = recorded
            errors.extend(recorded_errors)
            if source_version is None and not recorded_errors:
                errors.append("unable to parse the app version at the recorded source revision")
        else:
            # Release mode (--require-current), or a source root outside Git:
            # the artifact must match the current source exactly.
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
        errors.extend(bootstrap_serial_errors(html))
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
