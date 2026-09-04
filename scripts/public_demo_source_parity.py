from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any


SOURCE_PARITY_SCHEMA = 1
SOURCE_PARITY_PREFIX = "<!-- public-demo-source-parity "
SOURCE_PARITY_SUFFIX = " -->\n"
SOURCE_INPUT_PATHS: tuple[Path, ...] = (
    Path("app/services/public_demo_fixture.py"),
    Path("app/services/snapshot_export.py"),
    Path("app/static/app.js"),
    Path("app/static/style.css"),
    Path("app/templates/base.html"),
    Path("app/templates/index.html"),
    Path("app/static/images/aoc-slg4-2h8m2.jpg"),
    Path("app/static/images/hyper-m2-gen3-card.png"),
    Path("app/static/images/satadom-ml-3ie3-v2.png"),
)
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

    source_digests = _source_digests(source_root)
    inline_errors = _inline_source_errors(html, source_root)
    if inline_errors:
        raise ValueError("; ".join(inline_errors))

    manifest = {
        "artifact_sha256": _sha256_text(html),
        "schema": SOURCE_PARITY_SCHEMA,
        "source_output_sha256": _source_output_digest(source_digests, html),
        "sources": source_digests,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return f"{SOURCE_PARITY_PREFIX}{payload}{SOURCE_PARITY_SUFFIX}{html}"


def check_source_parity_manifest(html: str, *, source_root: Path) -> list[str]:
    manifest, artifact_html, parse_errors = _parse_manifest(html)
    if parse_errors:
        return parse_errors

    errors: list[str] = []
    if manifest.get("schema") != SOURCE_PARITY_SCHEMA:
        errors.append(
            "unsupported public demo source parity schema: "
            f"{manifest.get('schema')!r}"
        )

    expected_paths = tuple(path.as_posix() for path in SOURCE_INPUT_PATHS)
    declared_sources = manifest.get("sources")
    if not isinstance(declared_sources, dict):
        errors.append("invalid public demo source parity source map")
        declared_sources = {}
    else:
        declared_paths = tuple(sorted(declared_sources))
        if declared_paths != tuple(sorted(expected_paths)):
            errors.append(
                "public demo source parity input set mismatch: "
                f"expected {', '.join(expected_paths)}"
            )

    actual_source_digests: dict[str, str] = {}
    for relative_path in SOURCE_INPUT_PATHS:
        source_key = relative_path.as_posix()
        source_file = source_root / relative_path
        if not source_file.is_file():
            errors.append(f"missing authoritative source input: {source_key}")
            continue
        actual_digest = _sha256_bytes(source_file.read_bytes())
        actual_source_digests[source_key] = actual_digest
        if declared_sources.get(source_key) != actual_digest:
            errors.append(f"source fingerprint mismatch: {source_key}")

    errors.extend(_inline_source_errors(artifact_html, source_root))

    artifact_digest = manifest.get("artifact_sha256")
    if not isinstance(artifact_digest, str) or artifact_digest != _sha256_text(artifact_html):
        errors.append("public demo embedded output fingerprint mismatch")

    source_output_digest = manifest.get("source_output_sha256")
    if (
        not isinstance(source_output_digest, str)
        or source_output_digest != _source_output_digest(actual_source_digests, artifact_html)
    ):
        errors.append("source/output parity fingerprint mismatch")

    return errors


def _source_digests(source_root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for relative_path in SOURCE_INPUT_PATHS:
        source_file = source_root / relative_path
        if not source_file.is_file():
            raise ValueError(f"missing authoritative source input: {relative_path.as_posix()}")
        digests[relative_path.as_posix()] = _sha256_bytes(source_file.read_bytes())
    return digests


def _inline_source_errors(html: str, source_root: Path) -> list[str]:
    errors: list[str] = []
    for relative_path, (prefix, suffix) in INLINE_SOURCE_WRAPPERS.items():
        source_file = source_root / relative_path
        if not source_file.is_file():
            continue
        source_text = _inline_offline_images(
            source_file.read_text(encoding="utf-8"),
            source_root=source_root,
        )
        normalized_source = _normalize_embedded_source(source_text)
        expected_inline = f"{prefix}{normalized_source}{suffix}"
        if html.count(expected_inline) != 1:
            errors.append(f"embedded source mismatch: {relative_path.as_posix()}")
    return errors


def _normalize_embedded_source(source: str) -> str:
    trailing_newline = "\n" if source.endswith("\n") else ""
    return "\n".join(line.rstrip() for line in source.splitlines()) + trailing_newline


def _inline_offline_images(source: str, *, source_root: Path) -> str:
    for relative_path, mime_type in OFFLINE_IMAGE_INPUTS.items():
        encoded = base64.b64encode((source_root / relative_path).read_bytes()).decode("ascii")
        static_path = relative_path.relative_to("app/static").as_posix()
        data_url = f"data:{mime_type};base64,{encoded}"
        source = source.replace(f'"/static/{static_path}"', f'"{data_url}"')
        source = source.replace(f"'/static/{static_path}'", f"'{data_url}'")
    return source


def _parse_manifest(html: str) -> tuple[dict[str, Any], str, list[str]]:
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


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _source_output_digest(source_digests: dict[str, str], html: str) -> str:
    source_payload = json.dumps(source_digests, sort_keys=True, separators=(",", ":"))
    return _sha256_text(f"{source_payload}\0{html}")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
