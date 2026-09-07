from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.public_demo_inputs import PUBLIC_DEMO_INPUT_PATHS


SOURCE_PARITY_SCHEMA = 2
SOURCE_PARITY_PREFIX = "<!-- public-demo-source-parity "
SOURCE_PARITY_SUFFIX = " -->\n"
OFFLINE_IMAGE_INPUTS: dict[Path, str] = {
    Path("app/static/images/aoc-slg4-2h8m2.jpg"): "image/jpeg",
    Path("app/static/images/hyper-m2-gen3-card.png"): "image/png",
    Path("app/static/images/satadom-ml-3ie3-v2.png"): "image/png",
}
INLINE_SOURCE_WRAPPERS: dict[Path, tuple[str, str]] = {
    Path("app/static/app.js"): ("<script>\n", "\n</script>"),
    Path("app/static/style.css"): ("<style>\n", "\n</style>"),
}


def add_source_parity_manifest(html: str, *, source_root: Path) -> str:
    if html.startswith(SOURCE_PARITY_PREFIX):
        raise ValueError("public demo source parity manifest already exists")
    digests = source_digests(source_root)
    inline_errors = inline_source_errors(html, source_root)
    if inline_errors:
        raise ValueError("; ".join(inline_errors))
    manifest = {
        "artifact_sha256": sha256_text(html),
        "schema": SOURCE_PARITY_SCHEMA,
        "source_output_sha256": source_output_digest(digests, html),
        "sources": digests,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return f"{SOURCE_PARITY_PREFIX}{payload}{SOURCE_PARITY_SUFFIX}{html}"


def check_source_parity_manifest(html: str, *, source_root: Path) -> list[str]:
    manifest, artifact_html, parse_errors = parse_manifest(html)
    if parse_errors:
        return parse_errors
    errors: list[str] = []
    if manifest.get("schema") != SOURCE_PARITY_SCHEMA:
        errors.append(f"unsupported public demo source parity schema: {manifest.get('schema')!r}")

    expected_paths = tuple(path.as_posix() for path in PUBLIC_DEMO_INPUT_PATHS)
    declared_sources = manifest.get("sources")
    if not isinstance(declared_sources, dict):
        errors.append("invalid public demo source parity source map")
        declared_sources = {}
    elif tuple(sorted(declared_sources)) != tuple(sorted(expected_paths)):
        errors.append("public demo source parity input set mismatch")

    actual_source_digests: dict[str, str] = {}
    for relative_path in PUBLIC_DEMO_INPUT_PATHS:
        source_key = relative_path.as_posix()
        source_file = source_root / relative_path
        if not source_file.is_file():
            errors.append(f"missing authoritative source input: {source_key}")
            continue
        actual_digest = sha256_bytes(source_file.read_bytes())
        actual_source_digests[source_key] = actual_digest
        if declared_sources.get(source_key) != actual_digest:
            errors.append(f"source fingerprint mismatch: {source_key}")

    errors.extend(inline_source_errors(artifact_html, source_root))
    artifact_digest = manifest.get("artifact_sha256")
    if not isinstance(artifact_digest, str) or artifact_digest != sha256_text(artifact_html):
        errors.append("public demo embedded output fingerprint mismatch")
    combined_digest = manifest.get("source_output_sha256")
    if not isinstance(combined_digest, str) or combined_digest != source_output_digest(
        actual_source_digests,
        artifact_html,
    ):
        errors.append("source/output parity fingerprint mismatch")
    return errors


def source_digests(source_root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for relative_path in PUBLIC_DEMO_INPUT_PATHS:
        source_file = source_root / relative_path
        if not source_file.is_file():
            raise ValueError(f"missing authoritative source input: {relative_path.as_posix()}")
        digests[relative_path.as_posix()] = sha256_bytes(source_file.read_bytes())
    return digests


def inline_source_errors(html: str, source_root: Path) -> list[str]:
    errors: list[str] = []
    for relative_path, (prefix, suffix) in INLINE_SOURCE_WRAPPERS.items():
        source_file = source_root / relative_path
        if not source_file.is_file():
            continue
        source_text = inline_offline_images(source_file.read_text(encoding="utf-8"), source_root=source_root)
        expected_inline = f"{prefix}{normalize_embedded_source(source_text)}{suffix}"
        if html.count(expected_inline) != 1:
            errors.append(f"embedded source mismatch: {relative_path.as_posix()}")
    return errors


def normalize_embedded_source(source: str) -> str:
    trailing_newline = "\n" if source.endswith("\n") else ""
    return "\n".join(line.rstrip() for line in source.splitlines()) + trailing_newline


def inline_offline_images(source: str, *, source_root: Path) -> str:
    for relative_path, mime_type in OFFLINE_IMAGE_INPUTS.items():
        encoded = base64.b64encode((source_root / relative_path).read_bytes()).decode("ascii")
        static_path = relative_path.relative_to("app/static").as_posix()
        data_url = f"data:{mime_type};base64,{encoded}"
        source = source.replace(f'"/static/{static_path}"', f'"{data_url}"')
        source = source.replace(f"'/static/{static_path}'", f"'{data_url}'")
    return source


def parse_manifest(html: str) -> tuple[dict[str, Any], str, list[str]]:
    if not html.startswith(SOURCE_PARITY_PREFIX):
        return {}, html, ["missing public demo source parity manifest"]
    end = html.find(SOURCE_PARITY_SUFFIX, len(SOURCE_PARITY_PREFIX))
    if end < 0:
        return {}, html, ["malformed public demo source parity manifest"]
    payload = html[len(SOURCE_PARITY_PREFIX):end]
    artifact_html = html[end + len(SOURCE_PARITY_SUFFIX):]
    if artifact_html.startswith(SOURCE_PARITY_PREFIX):
        return {}, artifact_html, ["multiple public demo source parity manifests"]
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError:
        return {}, artifact_html, ["malformed public demo source parity manifest"]
    if not isinstance(manifest, dict):
        return {}, artifact_html, ["invalid public demo source parity manifest"]
    return manifest, artifact_html, []


def source_output_digest(source_hashes: dict[str, str], html: str) -> str:
    source_payload = json.dumps(source_hashes, sort_keys=True, separators=(",", ":"))
    return sha256_text(f"{source_payload}\0{html}")


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
