#!/usr/bin/env python3
"""Perform a digest-pinned Compose update with a verified rollback receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


REPOSITORY = "ghcr.io/gcs8/truenas-jbod-ui"
RECEIPT_DIR_NAME = ".jbod-ui-image-update"
RECEIPT_SCHEMA = 1
MAX_COMPOSE_BYTES = 2 * 1024 * 1024
DIGEST_PATTERN = re.compile(rf"^{re.escape(REPOSITORY)}@sha256:[0-9a-f]{{64}}$")
TAG_PATTERN = re.compile(rf"^{re.escape(REPOSITORY)}:[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SERVICE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SOURCE_PATTERN = re.compile(r"^docker-compose(?:\.[A-Za-z0-9_-]+)?\.yml$")
RunCommand = Callable[..., str]
Download = Callable[[str], bytes]
Probe = Callable[[str], None]
JsonObject = dict[str, Any]


class DeploymentError(RuntimeError):
    """A bounded update, verification, or rollback failure."""


@dataclass(frozen=True)
class ComposeFile:
    source: str
    live: str


@dataclass(frozen=True)
class DeploymentSpec:
    root: Path
    project_name: str
    source_revision: str
    expected_image: str
    candidate_tag: str
    compose_files: tuple[ComposeFile, ...]
    profiles: tuple[str, ...]
    services: tuple[str, ...]
    health_urls: tuple[str, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _require_private_directory(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DeploymentError(f"receipt path is not a real directory: {path.name}")
    if info.st_uid != os.geteuid():
        raise DeploymentError("receipt directory owner does not match the effective user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise DeploymentError("receipt directory must have mode 0700")


def _require_private_file(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DeploymentError(f"receipt entry is not a regular file: {path.name}")
    if info.st_uid != os.geteuid():
        raise DeploymentError("receipt file owner does not match the effective user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise DeploymentError("receipt files must have mode 0600")


def _json_without_duplicates(data: bytes) -> object:
    def pairs(values: list[tuple[str, Any]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in values:
            if key in result:
                raise DeploymentError(f"duplicate receipt key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("receipt JSON is invalid") from exc


def _write_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _replace_private_file(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_write_live(path: Path, data: bytes, mode: int) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise DeploymentError(f"live deployment path is not a regular file: {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _default_run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    command_env = None
    if env:
        command_env = os.environ.copy()
        command_env.update(env)
    if command[:2] == ["docker", "pull"]:
        phase, timeout = "image pull", 600
    elif command[:2] == ["docker", "compose"] and "pull" in command:
        phase, timeout = "Compose image pull", 600
    elif command[:2] == ["docker", "compose"] and "up" in command:
        phase, timeout = "Compose activation", 300
    elif command[:3] == ["docker", "compose", "--project-name"] and "config" in command:
        phase, timeout = "Compose validation", 180
    else:
        phase, timeout = "runtime inspection", 180
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeploymentError(f"{phase} timed out after {timeout} seconds") from exc
    if result.returncode:
        label = " ".join(command[:3])
        raise DeploymentError(f"command failed with exit {result.returncode}: {label}")
    return result.stdout.strip()


def _default_download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(MAX_COMPOSE_BYTES + 1)
    except OSError as exc:
        raise DeploymentError("candidate Compose download failed") from exc
    if len(data) > MAX_COMPOSE_BYTES:
        raise DeploymentError("candidate Compose file exceeds the 2 MiB limit")
    return data


def _default_probe(url: str) -> None:
    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: object,
            code: int,
            msg: str,
            headers: object,
            newurl: str,
        ) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirectHandler())
    last_error: Exception | None = None
    for _ in range(30):
        try:
            with opener.open(url, timeout=5) as response:
                if response.geturl() != url:
                    raise DeploymentError("health probe response URL changed")
                if 200 <= response.status < 300:
                    return
        except urllib.error.HTTPError as exc:
            exc.close()
            last_error = exc
        except OSError as exc:
            last_error = exc
        time.sleep(2)
    raise DeploymentError(f"health probe did not converge: {url}") from last_error


def _validate_name(value: str, *, label: str, pattern: re.Pattern[str] = NAME_PATTERN) -> None:
    if not pattern.fullmatch(value):
        raise DeploymentError(f"invalid {label}: {value!r}")


def _validate_health_url(value: str, *, label: str = "health URL") -> None:
    match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):([0-9]{1,5})/[^\s]*", value)
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise DeploymentError(f"{label} must use an explicit loopback HTTP endpoint")


def _validate_spec(spec: DeploymentSpec) -> Path:
    root = spec.root.resolve(strict=True)
    if spec.root.is_symlink() or not root.is_dir():
        raise DeploymentError("deployment root must be a real directory")
    if not REVISION_PATTERN.fullmatch(spec.source_revision):
        raise DeploymentError("source revision must be exactly 40 lowercase hex characters")
    if not DIGEST_PATTERN.fullmatch(spec.expected_image):
        raise DeploymentError("expected image must be the full repository-qualified digest")
    if not TAG_PATTERN.fullmatch(spec.candidate_tag):
        raise DeploymentError("candidate tag must be a repository-qualified GHCR tag")
    _validate_name(spec.project_name, label="project name", pattern=PROJECT_PATTERN)
    if not spec.compose_files:
        raise DeploymentError("at least one Compose file is required")
    if not spec.services:
        raise DeploymentError("at least one expected running service is required")
    if not spec.health_urls:
        raise DeploymentError("at least one health URL is required")
    sources: set[str] = set()
    live_names: set[str] = set()
    for item in spec.compose_files:
        _validate_name(item.source, label="Compose source", pattern=SOURCE_PATTERN)
        _validate_name(item.live, label="live Compose filename")
        if item.source in sources or item.live in live_names:
            raise DeploymentError("Compose source and live filenames must be unique")
        sources.add(item.source)
        live_names.add(item.live)
        live = root / item.live
        if live.is_symlink() or not live.is_file():
            raise DeploymentError(f"live Compose file is missing or unsafe: {item.live}")
    for profile in spec.profiles:
        _validate_name(profile, label="profile", pattern=SERVICE_PATTERN)
    if len(set(spec.profiles)) != len(spec.profiles):
        raise DeploymentError("profiles must be unique")
    for service in spec.services:
        _validate_name(service, label="service", pattern=SERVICE_PATTERN)
    if len(set(spec.services)) != len(spec.services):
        raise DeploymentError("services must be unique")
    for url in spec.health_urls:
        _validate_health_url(url)
    env_path = root / ".env"
    if env_path.is_symlink() or not env_path.is_file():
        raise DeploymentError(".env must be a regular non-symlink file")
    if _mode(env_path) & 0o077:
        raise DeploymentError(".env must not grant group or other permissions")
    return root


def _compose_prefix(
    root: Path,
    project_name: str,
    compose_files: list[str],
    profiles: list[str],
) -> list[str]:
    command = ["docker", "compose", "--project-name", project_name]
    for name in compose_files:
        command.extend(["-f", str(root / name)])
    for profile in profiles:
        command.extend(["--profile", profile])
    return command


def _parse_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def _inspect_container(run: RunCommand, root: Path, container: str) -> JsonObject:
    def inspect(template: str) -> str:
        return run(["docker", "inspect", "--format", template, container], cwd=root).strip()

    restart_text = inspect("{{.RestartCount}}")
    try:
        restart_count = int(restart_text)
    except ValueError as exc:
        raise DeploymentError("container restart count is not an integer") from exc
    config_files = inspect('{{index .Config.Labels "com.docker.compose.project.config_files"}}')
    return {
        "container": container,
        "image_id": inspect("{{.Image}}"),
        "status": inspect("{{.State.Status}}"),
        "health": inspect("{{if .State.Health}}{{.State.Health.Status}}{{end}}"),
        "restart_count": restart_count,
        "project_name": inspect('{{index .Config.Labels "com.docker.compose.project"}}'),
        "working_dir": inspect('{{index .Config.Labels "com.docker.compose.project.working_dir"}}'),
        "config_files": config_files.split(",") if config_files else [],
    }


def _validate_container_compose_contract(
    row: JsonObject,
    root: Path,
    project_name: str,
    compose_names: list[str],
) -> None:
    if row["project_name"] != project_name:
        raise DeploymentError(f"container Compose project does not match: {row['service']}")
    if row["working_dir"] != str(root):
        raise DeploymentError(f"container Compose working directory does not match: {row['service']}")
    expected_files = [str(root / name) for name in compose_names]
    if row["config_files"] != expected_files:
        raise DeploymentError(f"container Compose chain does not match: {row['service']}")


def _resolve_digest(run: RunCommand, root: Path, image: str) -> str:
    output = run(
        ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
        cwd=root,
    )
    try:
        values = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("Docker returned invalid RepoDigests JSON") from exc
    if not isinstance(values, list):
        raise DeploymentError("Docker RepoDigests metadata must be a JSON list")
    matches = sorted({value for value in values if isinstance(value, str) and DIGEST_PATTERN.fullmatch(value)})
    if len(matches) != 1:
        raise DeploymentError("image must expose exactly one matching GHCR RepoDigest")
    return matches[0]


def _image_id(run: RunCommand, root: Path, image: str) -> str:
    value = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], cwd=root).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise DeploymentError("Docker returned an invalid image ID")
    return value


def _replace_image_reference(data: bytes, image: str) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeploymentError(".env must be UTF-8") from exc
    lines = text.splitlines(keepends=True)
    indexes = [index for index, line in enumerate(lines) if line.startswith("JBOD_UI_IMAGE=")]
    if len(indexes) != 1:
        raise DeploymentError(".env must contain exactly one JBOD_UI_IMAGE assignment")
    ending = "\n" if lines[indexes[0]].endswith("\n") else ""
    lines[indexes[0]] = f"JBOD_UI_IMAGE={image}{ending}"
    return "".join(lines).encode("utf-8")


def _capture_previous_runtime(
    spec: DeploymentSpec,
    root: Path,
    run: RunCommand,
) -> tuple[str, list[JsonObject]]:
    compose_names = [item.live for item in spec.compose_files]
    prefix = _compose_prefix(root, spec.project_name, compose_names, list(spec.profiles))
    run([*prefix, "config", "--quiet"], cwd=root)
    running = sorted(_parse_lines(run([*prefix, "ps", "--services", "--status", "running"], cwd=root)))
    expected = sorted(spec.services)
    if running != expected:
        raise DeploymentError(f"running service set does not match the declared service set: {running!r}")
    rows: list[JsonObject] = []
    digests: set[str] = set()
    for service in spec.services:
        containers = _parse_lines(run([*prefix, "ps", "-q", service], cwd=root))
        if len(containers) != 1:
            raise DeploymentError(f"service must resolve to exactly one container: {service}")
        row = {"service": service, **_inspect_container(run, root, containers[0])}
        _validate_container_compose_contract(row, root, spec.project_name, compose_names)
        if row["status"] != "running":
            raise DeploymentError(f"expected service is not running: {service}")
        if row["health"] and row["health"] != "healthy":
            raise DeploymentError(f"current service health is not healthy: {service}")
        digest = _resolve_digest(run, root, str(row["image_id"]))
        row["digest"] = digest
        digests.add(digest)
        rows.append(row)
    if len(digests) != 1:
        raise DeploymentError("declared services do not share one rollback digest")
    return next(iter(digests)), rows


def _receipt_payload(
    spec: DeploymentSpec,
    previous_image: str,
    previous_services: list[JsonObject],
    hashes: dict[str, str],
    modes: dict[str, int],
) -> JsonObject:
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "prepared",
        "project_name": spec.project_name,
        "source_revision": spec.source_revision,
        "expected_image": spec.expected_image,
        "candidate_tag": spec.candidate_tag,
        "compose_files": [item.__dict__ for item in spec.compose_files],
        "profiles": list(spec.profiles),
        "services": list(spec.services),
        "health_urls": list(spec.health_urls),
        "previous_image": previous_image,
        "previous_services": previous_services,
        "hashes": hashes,
        "modes": modes,
        "result": None,
    }


def _write_receipt(receipt_dir: Path, receipt: JsonObject) -> None:
    data = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path = receipt_dir / "receipt.json"
    if path.exists():
        _replace_private_file(path, data)
    else:
        _write_private_file(path, data)
    _fsync_directory(receipt_dir)


def _prepare_receipt(
    spec: DeploymentSpec,
    root: Path,
    previous_image: str,
    previous_services: list[JsonObject],
    candidate_files: dict[str, bytes],
) -> tuple[Path, JsonObject]:
    staging = Path(tempfile.mkdtemp(prefix=f".{RECEIPT_DIR_NAME}.pending-", dir=root))
    os.chmod(staging, 0o700)
    try:
        for name in ("previous", "candidate"):
            (staging / name).mkdir(mode=0o700)
        hashes: dict[str, str] = {}
        modes: dict[str, int] = {}
        env_path = root / ".env"
        previous_env = env_path.read_bytes()
        candidate_env = _replace_image_reference(previous_env, spec.expected_image)
        modes[".env"] = _mode(env_path)
        for side, data in (("previous", previous_env), ("candidate", candidate_env)):
            relative = f"{side}/.env"
            _write_private_file(staging / relative, data)
            hashes[relative] = _sha256(data)
        for item in spec.compose_files:
            live_path = root / item.live
            previous_data = live_path.read_bytes()
            modes[item.live] = _mode(live_path)
            for side, data in (("previous", previous_data), ("candidate", candidate_files[item.live])):
                relative = f"{side}/{item.live}"
                _write_private_file(staging / relative, data)
                hashes[relative] = _sha256(data)
        receipt = _receipt_payload(spec, previous_image, previous_services, hashes, modes)
        _write_receipt(staging, receipt)
        final = root / RECEIPT_DIR_NAME
        os.replace(staging, final)
        _fsync_directory(root)
        return final, receipt
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_receipt_shape(receipt: object) -> JsonObject:
    if not isinstance(receipt, dict):
        raise DeploymentError("receipt must be a JSON object")
    expected_keys = {
        "schema",
        "status",
        "project_name",
        "source_revision",
        "expected_image",
        "candidate_tag",
        "compose_files",
        "profiles",
        "services",
        "health_urls",
        "previous_image",
        "previous_services",
        "hashes",
        "modes",
        "result",
    }
    if set(receipt) != expected_keys:
        raise DeploymentError("receipt key set does not match the schema")
    if type(receipt["schema"]) is not int or receipt["schema"] != RECEIPT_SCHEMA:
        raise DeploymentError("receipt schema is unsupported")
    if receipt["status"] not in {"prepared", "active", "rolled_back"}:
        raise DeploymentError("receipt status is invalid")
    if not isinstance(receipt["project_name"], str) or not PROJECT_PATTERN.fullmatch(receipt["project_name"]):
        raise DeploymentError("receipt project name is invalid")
    if not isinstance(receipt["source_revision"], str) or not REVISION_PATTERN.fullmatch(receipt["source_revision"]):
        raise DeploymentError("receipt source revision is invalid")
    for key in ("expected_image", "previous_image"):
        if not isinstance(receipt[key], str) or not DIGEST_PATTERN.fullmatch(receipt[key]):
            raise DeploymentError(f"receipt {key} is invalid")
    if not isinstance(receipt["candidate_tag"], str) or not TAG_PATTERN.fullmatch(receipt["candidate_tag"]):
        raise DeploymentError("receipt candidate tag is invalid")
    for key in ("profiles", "services", "health_urls"):
        values = receipt[key]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise DeploymentError(f"receipt {key} must be a string list")
        if len(values) != len(set(values)):
            raise DeploymentError(f"receipt {key} contains duplicates")
    for profile in receipt["profiles"]:
        if not SERVICE_PATTERN.fullmatch(profile):
            raise DeploymentError("receipt profile is invalid")
    for service in receipt["services"]:
        if not SERVICE_PATTERN.fullmatch(service):
            raise DeploymentError("receipt service is invalid")
    for url in receipt["health_urls"]:
        _validate_health_url(url, label="receipt health URL")
    if not receipt["health_urls"]:
        raise DeploymentError("receipt must contain at least one health URL")
    compose_files = receipt["compose_files"]
    if not isinstance(compose_files, list) or not compose_files:
        raise DeploymentError("receipt Compose chain is empty")
    sources: set[str] = set()
    lives: set[str] = set()
    for item in compose_files:
        if not isinstance(item, dict) or set(item) != {"source", "live"}:
            raise DeploymentError("receipt Compose entry is invalid")
        source = item["source"]
        live = item["live"]
        if not isinstance(source, str) or not SOURCE_PATTERN.fullmatch(source):
            raise DeploymentError("receipt Compose source is invalid")
        if not isinstance(live, str) or not NAME_PATTERN.fullmatch(live):
            raise DeploymentError("receipt live Compose filename is invalid")
        if source in sources or live in lives:
            raise DeploymentError("receipt Compose chain contains duplicates")
        sources.add(source)
        lives.add(live)
    previous_services = receipt["previous_services"]
    if not isinstance(previous_services, list) or len(previous_services) != len(receipt["services"]):
        raise DeploymentError("receipt previous service cardinality is invalid")
    service_names: list[str] = []
    for row in previous_services:
        expected_row_keys = {
            "service",
            "container",
            "image_id",
            "status",
            "health",
            "restart_count",
            "digest",
            "project_name",
            "working_dir",
            "config_files",
        }
        if not isinstance(row, dict) or set(row) != expected_row_keys:
            raise DeploymentError("receipt previous service entry is invalid")
        if not isinstance(row["service"], str) or row["service"] not in receipt["services"]:
            raise DeploymentError("receipt previous service name is invalid")
        if type(row["restart_count"]) is not int or row["restart_count"] < 0:
            raise DeploymentError("receipt restart count is invalid")
        if not isinstance(row["container"], str) or not row["container"] or "\n" in row["container"]:
            raise DeploymentError("receipt previous container ID is invalid")
        if not isinstance(row["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"]):
            raise DeploymentError("receipt previous image ID is invalid")
        if row["status"] != "running":
            raise DeploymentError("receipt previous service status is invalid")
        if row["health"] not in {"", "healthy"}:
            raise DeploymentError("receipt previous service health is invalid")
        if not isinstance(row["project_name"], str) or not PROJECT_PATTERN.fullmatch(row["project_name"]):
            raise DeploymentError("receipt previous project name is invalid")
        if not isinstance(row["working_dir"], str) or not row["working_dir"]:
            raise DeploymentError("receipt previous working directory is invalid")
        if not isinstance(row["config_files"], list) or not all(
            isinstance(value, str) and value for value in row["config_files"]
        ):
            raise DeploymentError("receipt previous Compose chain is invalid")
        if not isinstance(row["digest"], str) or row["digest"] != receipt["previous_image"]:
            raise DeploymentError("receipt previous service digest is inconsistent")
        service_names.append(row["service"])
    if sorted(service_names) != sorted(receipt["services"]):
        raise DeploymentError("receipt previous service set is incomplete")
    if not isinstance(receipt["hashes"], dict) or not isinstance(receipt["modes"], dict):
        raise DeploymentError("receipt hashes and modes must be objects")
    result = receipt["result"]
    if receipt["status"] == "prepared":
        if result is not None:
            raise DeploymentError("prepared receipt result must be null")
    else:
        if not isinstance(result, dict) or set(result) != {"image", "image_id", "services", "health_urls"}:
            raise DeploymentError("receipt result does not match the schema")
        expected_image = receipt["expected_image"] if receipt["status"] == "active" else receipt["previous_image"]
        if result["image"] != expected_image:
            raise DeploymentError("receipt result image is inconsistent")
        if not isinstance(result["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", result["image_id"]):
            raise DeploymentError("receipt result image ID is invalid")
        if result["health_urls"] != receipt["health_urls"]:
            raise DeploymentError("receipt result health URLs are inconsistent")
        result_services = result["services"]
        if not isinstance(result_services, list) or len(result_services) != len(receipt["services"]):
            raise DeploymentError("receipt result service cardinality is invalid")
        result_names: list[str] = []
        for row in result_services:
            expected_result_keys = {
                "service",
                "container",
                "image_id",
                "status",
                "health",
                "restart_count",
                "project_name",
                "working_dir",
                "config_files",
            }
            if not isinstance(row, dict) or set(row) != expected_result_keys:
                raise DeploymentError("receipt result service entry is invalid")
            if row["service"] not in receipt["services"]:
                raise DeploymentError("receipt result service name is invalid")
            if not isinstance(row["container"], str) or not row["container"] or "\n" in row["container"]:
                raise DeploymentError("receipt result container ID is invalid")
            if row["image_id"] != result["image_id"]:
                raise DeploymentError("receipt result service image ID is inconsistent")
            if row["status"] != "running" or row["health"] not in {"", "healthy"}:
                raise DeploymentError("receipt result service state is invalid")
            if type(row["restart_count"]) is not int or row["restart_count"] != 0:
                raise DeploymentError("receipt result restart count is invalid")
            if not isinstance(row["project_name"], str) or not PROJECT_PATTERN.fullmatch(row["project_name"]):
                raise DeploymentError("receipt result project name is invalid")
            if not isinstance(row["working_dir"], str) or not row["working_dir"]:
                raise DeploymentError("receipt result working directory is invalid")
            if not isinstance(row["config_files"], list) or not all(
                isinstance(value, str) and value for value in row["config_files"]
            ):
                raise DeploymentError("receipt result Compose chain is invalid")
            result_names.append(row["service"])
        if sorted(result_names) != sorted(receipt["services"]):
            raise DeploymentError("receipt result service set is incomplete")
    return receipt


def validate_receipt(root: Path) -> JsonObject:
    root = root.resolve(strict=True)
    receipt_dir = root / RECEIPT_DIR_NAME
    if not receipt_dir.exists():
        raise DeploymentError("deployment receipt does not exist")
    _require_private_directory(receipt_dir)
    receipt_path = receipt_dir / "receipt.json"
    _require_private_file(receipt_path)
    receipt = _validate_receipt_shape(_json_without_duplicates(receipt_path.read_bytes()))
    compose_names = [str(item["live"]) for item in receipt["compose_files"]]
    for row in receipt["previous_services"]:
        _validate_container_compose_contract(row, root, str(receipt["project_name"]), compose_names)
    if receipt["result"] is not None:
        for row in receipt["result"]["services"]:
            _validate_container_compose_contract(row, root, str(receipt["project_name"]), compose_names)
    expected_files = {"receipt.json", "previous/.env", "candidate/.env"}
    for name in compose_names:
        expected_files.add(f"previous/{name}")
        expected_files.add(f"candidate/{name}")
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for path in receipt_dir.rglob("*"):
        relative = path.relative_to(receipt_dir).as_posix()
        if path.is_symlink():
            raise DeploymentError("receipt contains a symlink")
        if path.is_dir():
            actual_dirs.add(relative)
            _require_private_directory(path)
        else:
            actual_files.add(relative)
            _require_private_file(path)
    if actual_dirs != {"previous", "candidate"} or actual_files != expected_files:
        raise DeploymentError("receipt file set does not match the recorded contract")
    expected_hashes = expected_files - {"receipt.json"}
    hashes = receipt["hashes"]
    if set(hashes) != expected_hashes:
        raise DeploymentError("receipt hash set does not match the recorded contract")
    for relative in expected_hashes:
        digest = hashes[relative]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DeploymentError("receipt file hash is invalid")
        if _sha256((receipt_dir / relative).read_bytes()) != digest:
            raise DeploymentError("receipt file hash verification failed")
    expected_modes = {".env", *compose_names}
    modes = receipt["modes"]
    if set(modes) != expected_modes:
        raise DeploymentError("receipt mode set does not match the recorded contract")
    if any(not isinstance(value, int) or value < 0o400 or value > 0o777 for value in modes.values()):
        raise DeploymentError("receipt live-file mode is invalid")
    return receipt


def _verify_runtime(
    root: Path,
    receipt: JsonObject,
    image: str,
    *,
    run: RunCommand,
    probe: Probe,
) -> JsonObject:
    compose_names = [str(item["live"]) for item in receipt["compose_files"]]
    profiles = [str(value) for value in receipt["profiles"]]
    services = [str(value) for value in receipt["services"]]
    prefix = _compose_prefix(root, str(receipt["project_name"]), compose_names, profiles)
    run([*prefix, "config", "--quiet"], cwd=root)
    running = sorted(_parse_lines(run([*prefix, "ps", "--services", "--status", "running"], cwd=root)))
    if running != sorted(services):
        raise DeploymentError(f"running service set does not match the recorded service set: {running!r}")
    expected_image_id = _image_id(run, root, image)
    runtime_rows: list[JsonObject] = []
    for service in services:
        containers = _parse_lines(run([*prefix, "ps", "-q", service], cwd=root))
        if len(containers) != 1:
            raise DeploymentError(f"service must resolve to exactly one container: {service}")
        row = {"service": service, **_inspect_container(run, root, containers[0])}
        _validate_container_compose_contract(row, root, str(receipt["project_name"]), compose_names)
        if row["status"] != "running":
            raise DeploymentError(f"expected service is not running: {service}")
        if row["image_id"] != expected_image_id:
            raise DeploymentError(f"service image ID did not converge: {service}")
        if row["health"] and row["health"] != "healthy":
            raise DeploymentError(f"service health did not converge: {service}")
        if row["restart_count"] != 0:
            raise DeploymentError(f"service restart count is nonzero after activation: {service}")
        runtime_rows.append(row)
    for url in receipt["health_urls"]:
        probe(str(url))
    return {"image": image, "image_id": expected_image_id, "services": runtime_rows, "health_urls": receipt["health_urls"]}


def _restore_previous(
    root: Path,
    receipt_dir: Path,
    receipt: JsonObject,
    *,
    run: RunCommand,
    probe: Probe,
) -> JsonObject:
    compose_names = [str(item["live"]) for item in receipt["compose_files"]]
    modes = receipt["modes"]
    for name in compose_names:
        _atomic_write_live(root / name, (receipt_dir / "previous" / name).read_bytes(), int(modes[name]))
    previous_env = (receipt_dir / "previous" / ".env").read_bytes()
    rollback_env = _replace_image_reference(previous_env, str(receipt["previous_image"]))
    _atomic_write_live(root / ".env", rollback_env, int(modes[".env"]))
    run(["docker", "pull", str(receipt["previous_image"])], cwd=root)
    prefix = _compose_prefix(
        root,
        str(receipt["project_name"]),
        compose_names,
        [str(value) for value in receipt["profiles"]],
    )
    services = [str(value) for value in receipt["services"]]
    run([*prefix, "pull", *services], cwd=root)
    run([*prefix, "up", "-d", *services], cwd=root)
    result = _verify_runtime(root, receipt, str(receipt["previous_image"]), run=run, probe=probe)
    receipt["status"] = "rolled_back"
    receipt["result"] = result
    _write_receipt(receipt_dir, receipt)
    return result


def update_deployment(
    spec: DeploymentSpec,
    *,
    run: RunCommand = _default_run,
    download: Download = _default_download,
    probe: Probe = _default_probe,
) -> JsonObject:
    root = _validate_spec(spec)
    receipt_dir = root / RECEIPT_DIR_NAME
    if receipt_dir.exists() or receipt_dir.is_symlink():
        raise DeploymentError("deployment receipt already exists; archive it before another update")
    previous_image, previous_services = _capture_previous_runtime(spec, root, run)
    run(["docker", "pull", spec.candidate_tag], cwd=root)
    candidate_digest = _resolve_digest(run, root, spec.candidate_tag)
    if candidate_digest != spec.expected_image:
        raise DeploymentError("candidate tag does not match the selected workflow receipt")
    candidate_files: dict[str, bytes] = {}
    for item in spec.compose_files:
        url = (
            f"https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/"
            f"{spec.source_revision}/{item.source}"
        )
        data = download(url)
        if not data or len(data) > MAX_COMPOSE_BYTES:
            raise DeploymentError(f"candidate Compose download is empty or too large: {item.source}")
        candidate_files[item.live] = data
    staging = Path(tempfile.mkdtemp(prefix=".jbod-ui-compose-check-", dir=root))
    os.chmod(staging, 0o700)
    try:
        for item in spec.compose_files:
            _write_private_file(staging / item.live, candidate_files[item.live])
        candidate_prefix = _compose_prefix(
            staging,
            spec.project_name,
            [item.live for item in spec.compose_files],
            list(spec.profiles),
        )
        run([*candidate_prefix, "config", "--quiet"], cwd=root, env={"JBOD_UI_IMAGE": spec.expected_image})
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    receipt_dir, receipt = _prepare_receipt(
        spec,
        root,
        previous_image,
        previous_services,
        candidate_files,
    )
    try:
        for item in spec.compose_files:
            _atomic_write_live(
                root / item.live,
                (receipt_dir / "candidate" / item.live).read_bytes(),
                int(receipt["modes"][item.live]),
            )
        _atomic_write_live(
            root / ".env",
            (receipt_dir / "candidate" / ".env").read_bytes(),
            int(receipt["modes"][".env"]),
        )
        prefix = _compose_prefix(
            root,
            spec.project_name,
            [item.live for item in spec.compose_files],
            list(spec.profiles),
        )
        run([*prefix, "pull", *spec.services], cwd=root)
        run([*prefix, "up", "-d", *spec.services], cwd=root)
        result = _verify_runtime(root, receipt, spec.expected_image, run=run, probe=probe)
        receipt["status"] = "active"
        receipt["result"] = result
        _write_receipt(receipt_dir, receipt)
        return {"status": "active", **result}
    except BaseException as activation_error:
        try:
            _restore_previous(root, receipt_dir, receipt, run=run, probe=probe)
        except BaseException as rollback_error:
            raise DeploymentError("activation failed and automatic rollback failed") from rollback_error
        raise DeploymentError("activation failed; automatic rollback completed") from activation_error


def verify_deployment(
    root: Path,
    *,
    run: RunCommand = _default_run,
    probe: Probe = _default_probe,
) -> JsonObject:
    root = root.resolve(strict=True)
    receipt = validate_receipt(root)
    if receipt["status"] == "prepared":
        raise DeploymentError("prepared receipt has no terminal deployment state")
    image = str(receipt["expected_image"] if receipt["status"] == "active" else receipt["previous_image"])
    return _verify_runtime(root, receipt, image, run=run, probe=probe)


def rollback_deployment(
    root: Path,
    *,
    run: RunCommand = _default_run,
    probe: Probe = _default_probe,
) -> JsonObject:
    root = root.resolve(strict=True)
    receipt = validate_receipt(root)
    return _restore_previous(root, root / RECEIPT_DIR_NAME, receipt, run=run, probe=probe)


def _parse_compose(value: str) -> ComposeFile:
    source, separator, live = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("Compose mappings use SOURCE=LIVE")
    return ComposeFile(source=source, live=live)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    update = subparsers.add_parser("update", help="activate one verified immutable image")
    update.add_argument("root", type=Path)
    update.add_argument("--project-name", required=True)
    update.add_argument("--source-revision", required=True)
    update.add_argument("--expected-image", required=True)
    update.add_argument("--candidate-tag", required=True)
    update.add_argument("--compose", action="append", type=_parse_compose, required=True)
    update.add_argument("--profile", action="append", default=[])
    update.add_argument("--service", action="append", required=True)
    update.add_argument("--health-url", action="append", required=True)
    for action in ("verify", "rollback"):
        command = subparsers.add_parser(action)
        command.add_argument("root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.action == "update":
            result = update_deployment(
                DeploymentSpec(
                    root=args.root,
                    project_name=args.project_name,
                    source_revision=args.source_revision,
                    expected_image=args.expected_image,
                    candidate_tag=args.candidate_tag,
                    compose_files=tuple(args.compose),
                    profiles=tuple(args.profile),
                    services=tuple(args.service),
                    health_urls=tuple(args.health_url),
                )
            )
        elif args.action == "verify":
            result = verify_deployment(args.root)
        else:
            result = rollback_deployment(args.root)
    except DeploymentError as exc:
        print(f"deployment error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
