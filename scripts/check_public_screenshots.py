from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NAMES = {
    "public-demo-history.png": 1920,
    "public-demo-overview.png": 1920,
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PRIVATE_IPV4_BYTES = re.compile(
    rb"(?<![0-9])(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))"
    rb"\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    rb"\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(?![0-9])"
)
PRIVATE_PATH_BYTES = re.compile(
    rb"(?:"
    + b"/" + b"home" + b"/"
    + rb"|"
    + b"/" + b"mnt" + b"/"
    + rb"|[A-Za-z]:\\Users\\)"
)
FORBIDDEN_BYTE_PATTERNS = (
    ("private IPv4 address", PRIVATE_IPV4_BYTES),
    ("private key marker", re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY")),
    ("local absolute path", PRIVATE_PATH_BYTES),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify exact public screenshot bytes and fixture provenance.")
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_png_size(payload: bytes) -> tuple[int, int]:
    if payload[:8] != PNG_SIGNATURE or payload[12:16] != b"IHDR" or len(payload) < 24:
        raise ValueError("invalid PNG framing")
    return struct.unpack(">II", payload[16:24])


def public_demo_source_revision(artifact: bytes) -> str:
    prefix = b"<!-- public-demo-source-parity "
    suffix = b" -->\n"
    if not artifact.startswith(prefix):
        raise ValueError("public demo source parity manifest is missing")
    end = artifact.find(suffix, len(prefix))
    if end < 0:
        raise ValueError("public demo source parity manifest is malformed")
    try:
        manifest = json.loads(artifact[len(prefix) : end])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("public demo source parity manifest is malformed") from exc
    revision = manifest.get("source_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("public demo source revision is invalid")
    return revision


def check_screenshots(root: Path) -> list[str]:
    errors: list[str] = []
    docs_root = root / "docs/images/screenshots"
    wiki_root = root / "wiki/images"
    manifest_path = docs_root / "manifest.json"
    if not manifest_path.is_file():
        return ["missing docs/images/screenshots/manifest.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["invalid docs/images/screenshots/manifest.json"]
    if set(manifest) != {
        "images",
        "provenance",
        "schema_version",
        "source_artifact_sha256",
        "source_revision",
    }:
        errors.append("screenshot manifest field set mismatch")
    if manifest.get("schema_version") != 1:
        errors.append("unsupported screenshot manifest schema")
    if manifest.get("provenance") != "synthetic-public-demo":
        errors.append("screenshot provenance must be synthetic-public-demo")

    artifact_path = root / "public-demo/index.html"
    artifact = artifact_path.read_bytes() if artifact_path.is_file() else b""
    if manifest.get("source_artifact_sha256") != sha256_bytes(artifact):
        errors.append("screenshot source artifact fingerprint mismatch")
    try:
        expected_revision = public_demo_source_revision(artifact)
    except ValueError as exc:
        errors.append(str(exc))
        expected_revision = None
    if manifest.get("source_revision") != expected_revision:
        errors.append("screenshot source revision mismatch")

    review_path = root / "docs/PUBLIC_SCREENSHOT_REVIEW.md"
    if not review_path.is_file():
        errors.append("missing docs/PUBLIC_SCREENSHOT_REVIEW.md")
        review = ""
    else:
        review = review_path.read_text(encoding="utf-8")
    for label, value in (
        ("source revision", manifest.get("source_revision")),
        ("source artifact fingerprint", manifest.get("source_artifact_sha256")),
    ):
        if not isinstance(value, str) or value not in review:
            errors.append(f"pixel review record is missing {label}")

    image_records = manifest.get("images")
    if not isinstance(image_records, dict) or set(image_records) != set(EXPECTED_NAMES):
        errors.append("screenshot manifest image set mismatch")
        image_records = {}
    docs_names = {path.name for path in docs_root.glob("*.png")}
    wiki_names = {path.name for path in wiki_root.glob("*.png")}
    if docs_names != set(EXPECTED_NAMES):
        errors.append("docs screenshot file set mismatch")
    if wiki_names != set(EXPECTED_NAMES):
        errors.append("Wiki screenshot file set mismatch")

    for name, expected_width in EXPECTED_NAMES.items():
        docs_path = docs_root / name
        wiki_path = wiki_root / name
        if not docs_path.is_file() or not wiki_path.is_file():
            continue
        docs_bytes = docs_path.read_bytes()
        wiki_bytes = wiki_path.read_bytes()
        if docs_bytes != wiki_bytes:
            errors.append(f"docs/Wiki screenshot byte mismatch: {name}")
        try:
            width, height = read_png_size(docs_bytes)
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if width != expected_width or height < 600 or height > 6000:
            errors.append(f"{name}: unexpected PNG dimensions {width}x{height}")
        record = image_records.get(name)
        if not isinstance(record, dict) or set(record) != {
            "bytes",
            "dimensions",
            "pixel_review",
            "sha256",
        }:
            errors.append(f"{name}: screenshot manifest record mismatch")
            continue
        record_sha256 = record.get("sha256")
        if not isinstance(record_sha256, str) or record_sha256 != sha256_bytes(docs_bytes):
            errors.append(f"{name}: screenshot SHA-256 mismatch")
        if name not in review or not isinstance(record_sha256, str) or record_sha256 not in review:
            errors.append(f"{name}: pixel review record does not bind the exact image")
        if record.get("bytes") != len(docs_bytes):
            errors.append(f"{name}: screenshot byte count mismatch")
        if record.get("dimensions") != [width, height]:
            errors.append(f"{name}: screenshot dimensions mismatch")
        if record.get("pixel_review") != "PASS":
            errors.append(f"{name}: exact-byte pixel review is not PASS")
        for label, pattern in FORBIDDEN_BYTE_PATTERNS:
            if pattern.search(docs_bytes):
                errors.append(f"{name}: found {label} in PNG bytes")
    return errors


def main() -> int:
    args = parse_args()
    errors = check_screenshots(args.root.resolve())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Public screenshots: PASS (2 fixture images, 4 image copies, exact-byte manifest and pixel review)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
