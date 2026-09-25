from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.public_demo_inputs import PUBLIC_DEMO_INPUT_PATHS  # noqa: E402


SOURCE_PARITY_SCHEMA = 3
SOURCE_PARITY_PREFIX = "<!-- public-demo-source-parity "
SOURCE_PARITY_SUFFIX = " -->\n"
SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
# The input declaration as it existed at a recorded revision. It is read as
# text, never imported, so an old revision's code is not executed.
RECORDED_INPUT_DECLARATION = Path("scripts/public_demo_inputs.py")
DECLARED_INPUT_PATTERN = re.compile(r'Path\("([^"\\]+)"\)')
RECORDED_VERSION_PATTERN = re.compile(
    r'^__version__\s*=\s*["\'](?P<version>[0-9A-Za-z][0-9A-Za-z.+-]*)["\']\s*$',
    re.MULTILINE,
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


def add_source_parity_manifest(
    html: str,
    *,
    source_root: Path,
    source_revision: str,
) -> str:
    if html.startswith(SOURCE_PARITY_PREFIX):
        raise ValueError("public demo source parity manifest already exists")
    source_revision = normalize_source_revision(source_revision)
    digests = source_digests(source_root)
    inline_errors = inline_source_errors(html, source_root)
    if inline_errors:
        raise ValueError("; ".join(inline_errors))
    build_id = build_identity(digests, source_revision)
    html = inject_visible_build_identity(
        html,
        source_revision=source_revision,
        build_id=build_id,
    )
    manifest = {
        "artifact_sha256": sha256_text(html),
        "build_id": build_id,
        "schema": SOURCE_PARITY_SCHEMA,
        "source_revision": source_revision,
        "source_output_sha256": source_output_digest(digests, html),
        "sources": digests,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return f"{SOURCE_PARITY_PREFIX}{payload}{SOURCE_PARITY_SUFFIX}{html}"


def check_source_parity_manifest(
    html: str,
    *,
    source_root: Path,
    input_paths: tuple[Path, ...] = PUBLIC_DEMO_INPUT_PATHS,
) -> list[str]:
    """Check the artifact against the declared inputs found under ``source_root``.

    ``input_paths`` defaults to the current declaration. The pull-request
    integrity check passes the input set recorded in the artifact, with
    ``source_root`` holding those inputs read back from the recorded revision.
    """
    manifest, artifact_html, parse_errors = parse_manifest(html)
    if parse_errors:
        return parse_errors
    errors: list[str] = []
    if manifest.get("schema") != SOURCE_PARITY_SCHEMA:
        errors.append(f"unsupported public demo source parity schema: {manifest.get('schema')!r}")

    expected_paths = tuple(path.as_posix() for path in input_paths)
    declared_sources = manifest.get("sources")
    if not isinstance(declared_sources, dict):
        errors.append("invalid public demo source parity source map")
        declared_sources = {}
    elif tuple(sorted(declared_sources)) != tuple(sorted(expected_paths)):
        errors.append("public demo source parity input set mismatch")

    source_revision = manifest.get("source_revision")
    if not isinstance(source_revision, str) or SOURCE_REVISION_PATTERN.fullmatch(source_revision) is None:
        errors.append("invalid public demo source revision")
        source_revision = ""

    actual_source_digests: dict[str, str] = {}
    for relative_path in input_paths:
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
    expected_build_id = build_identity(actual_source_digests, source_revision) if source_revision else ""
    build_id = manifest.get("build_id")
    if not isinstance(build_id, str) or build_id != expected_build_id:
        errors.append("public demo build identity mismatch")
    if source_revision and artifact_html.count(f">{source_revision}<") != 1:
        errors.append("public demo source revision is not visible exactly once")
    if expected_build_id and artifact_html.count(f">{expected_build_id}<") != 1:
        errors.append("public demo build identity is not visible exactly once")
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


def normalize_source_revision(value: str) -> str:
    revision = value.strip()
    if SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError("source revision must be a full lowercase 40-character Git commit")
    return revision


def build_identity(source_hashes: dict[str, str], source_revision: str) -> str:
    payload = {
        "schema": SOURCE_PARITY_SCHEMA,
        "source_revision": normalize_source_revision(source_revision),
        "sources": source_hashes,
    }
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def inject_visible_build_identity(html: str, *, source_revision: str, build_id: str) -> str:
    marker = (
        '      <div class="summary-card compact">\n'
        '        <span class="summary-label">Redaction</span>\n'
        '        <span class="summary-value">Synthetic IDs</span>\n'
        '        <span class="summary-note">Made-up disks, serials and hosts. Nothing here comes from a real system.</span>\n'
        "      </div>"
    )
    if html.count(marker) != 1:
        raise ValueError("public demo identity insertion point is missing or ambiguous")
    identity_cards = (
        "\n"
        '      <div class="summary-card compact">\n'
        '        <span class="summary-label">Source revision</span>\n'
        f'        <span class="summary-value public-demo-identity">{source_revision}</span>\n'
        '        <span class="summary-note">Git commit of the files this demo was built from</span>\n'
        "      </div>\n"
        '      <div class="summary-card compact">\n'
        '        <span class="summary-label">Build ID</span>\n'
        f'        <span class="summary-value public-demo-identity">{build_id}</span>\n'
        '        <span class="summary-note">fingerprint of that commit and the demo inputs</span>\n'
        "      </div>"
    )
    return html.replace(marker, marker + identity_cards, 1)


def _git_toplevel(source_root: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _git_show(source_root: Path, revision: str, relative_path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(source_root), "show", f"{revision}:{relative_path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _safe_relative_input(value: object) -> Path | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path


def recorded_source_integrity(html: str, *, source_root: Path) -> tuple[list[str], str | None] | None:
    """Check the artifact against the inputs at the revision it records.

    This is the pull-request check. It proves the checked-in artifact is an
    untampered build of a reachable commit, without requiring that commit to
    match the current source. Release preparation uses the stricter current
    source check instead (``check_public_demo_artifact.py --require-current``).

    Returns ``None`` when ``source_root`` is not the top of a Git checkout, so
    the caller can fall back to the strict working-tree comparison. Otherwise
    returns the errors and the app version recorded at that revision.
    """
    toplevel = _git_toplevel(source_root)
    if toplevel is None or toplevel != source_root.resolve():
        return None
    manifest, _artifact_html, parse_errors = parse_manifest(html)
    if parse_errors:
        return parse_errors, None
    try:
        revision = normalize_source_revision(str(manifest.get("source_revision", "")))
    except ValueError:
        return ["invalid public demo source revision"], None
    if subprocess.run(
        ["git", "-C", str(source_root), "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
        check=False,
    ).returncode != 0:
        return ["recorded public demo source revision is not a local commit"], None
    if subprocess.run(
        ["git", "-C", str(source_root), "merge-base", "--is-ancestor", revision, "HEAD"],
        capture_output=True,
        check=False,
    ).returncode != 0:
        return ["recorded public demo source revision is not an ancestor of HEAD"], None

    declaration = _git_show(source_root, revision, RECORDED_INPUT_DECLARATION.as_posix())
    if declaration is None:
        return ["recorded public demo input declaration is missing at the source revision"], None
    declared = {
        match.group(1)
        for match in DECLARED_INPUT_PATTERN.finditer(declaration.decode("utf-8", errors="replace"))
    }
    recorded_sources = manifest.get("sources")
    if not isinstance(recorded_sources, dict) or set(recorded_sources) != declared:
        return ["public demo source parity input set mismatch at the source revision"], None
    input_paths: list[Path] = []
    for key in sorted(declared):
        relative_path = _safe_relative_input(key)
        if relative_path is None:
            return [f"unsafe recorded public demo input path: {key!r}"], None
        input_paths.append(relative_path)

    with tempfile.TemporaryDirectory(prefix="public-demo-recorded-") as temp_dir:
        recorded_root = Path(temp_dir)
        errors: list[str] = []
        for relative_path in input_paths:
            payload = _git_show(source_root, revision, relative_path.as_posix())
            if payload is None:
                errors.append(f"missing authoritative source input at the source revision: {relative_path.as_posix()}")
                continue
            target = recorded_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        if errors:
            return errors, None
        try:
            errors.extend(
                check_source_parity_manifest(
                    html,
                    source_root=recorded_root,
                    input_paths=tuple(input_paths),
                )
            )
        except OSError as exc:
            errors.append(f"unable to read recorded public demo inputs: {exc}")
        version_source = (recorded_root / "app" / "__init__.py")
        recorded_version = None
        if version_source.is_file():
            match = RECORDED_VERSION_PATTERN.search(version_source.read_text(encoding="utf-8"))
            recorded_version = match.group("version") if match else None
        return errors, recorded_version


def recorded_source_revision_errors(*, source_root: Path, source_revision: str) -> list[str]:
    """Release check: the recorded revision is reachable and no input changed since.

    Used by ``check_public_demo_artifact.py --require-current``.
    """
    try:
        revision = normalize_source_revision(source_revision)
    except ValueError as exc:
        return [str(exc)]
    git_dir = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--git-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    if git_dir.returncode != 0:
        return []
    if subprocess.run(
        ["git", "-C", str(source_root), "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
        check=False,
    ).returncode != 0:
        return ["recorded public demo source revision is not a local commit"]
    if subprocess.run(
        ["git", "-C", str(source_root), "merge-base", "--is-ancestor", revision, "HEAD"],
        capture_output=True,
        check=False,
    ).returncode != 0:
        return ["recorded public demo source revision is not an ancestor of HEAD"]
    paths = [path.as_posix() for path in PUBLIC_DEMO_INPUT_PATHS]
    committed_drift = subprocess.run(
        ["git", "-C", str(source_root), "diff", "--quiet", f"{revision}..HEAD", "--", *paths],
        capture_output=True,
        check=False,
    ).returncode
    if committed_drift != 0:
        return ["declared public demo inputs changed after the recorded source revision"]
    working_drift = subprocess.run(
        ["git", "-C", str(source_root), "diff", "--quiet", "HEAD", "--", *paths],
        capture_output=True,
        check=False,
    ).returncode
    if working_drift != 0:
        return ["declared public demo inputs have uncommitted changes"]
    return []


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
