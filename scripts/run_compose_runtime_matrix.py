from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple, Sequence


APP_UID = 10001
APP_GID = 10001
BACKUP_UID = 1000
BACKUP_GID = 1000
AUTH_USERNAME = "operator"
AUTH_PASSWORD = "synthetic-compose-matrix-passphrase"
IMAGE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
MINIMUM_AVAILABLE_MEMORY_MIB = 3072
MINIMUM_FREE_DISK_GIB = 5
MATRIX_CONTAINER_NAMES = {
    "truenas-jbod-ui",
    "truenas-jbod-history",
    "truenas-jbod-admin",
}
SUCCESS_MARKER = "compose_runtime_matrix=ok variants=5 ui_alias_cycles=4 ui_mapping_cycles=4 admin_setup_cycles=1"


class Variant(NamedTuple):
    name: str
    profiles: tuple[str, ...]
    services: tuple[str, ...]
    ui_enabled: bool
    history_enabled: bool
    admin_enabled: bool
    admin_initial_setup: bool = False


class Ports(NamedTuple):
    ui: int
    history: int
    admin: int


VARIANTS = (
    Variant("ui-only", (), ("enclosure-ui",), True, False, False),
    Variant(
        "ui-history",
        ("history",),
        ("enclosure-ui", "enclosure-history"),
        True,
        True,
        False,
    ),
    Variant(
        "admin-only",
        ("admin",),
        ("enclosure-admin",),
        False,
        False,
        True,
        True,
    ),
    Variant(
        "ui-admin",
        ("admin",),
        ("enclosure-ui", "enclosure-admin"),
        True,
        False,
        True,
    ),
    Variant(
        "ui-history-admin",
        ("history", "admin"),
        ("enclosure-ui", "enclosure-history", "enclosure-admin"),
        True,
        True,
        True,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the synthetic Docker Compose service-combination matrix."
    )
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--config-fixture", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--ui-port", type=int, required=True)
    parser.add_argument("--history-port", type=int, required=True)
    parser.add_argument("--admin-port", type=int, required=True)
    parser.add_argument("--ack", required=True)
    return parser.parse_args()


def validate_runtime_root(runtime_root: Path, scratch_root: Path) -> Path:
    scratch_metadata = scratch_root.stat(follow_symlinks=False)
    if scratch_root.is_symlink() or not stat.S_ISDIR(scratch_metadata.st_mode):
        raise ValueError("Scratch root must be a real directory.")
    if stat.S_IMODE(scratch_metadata.st_mode) & 0o077:
        raise ValueError("Scratch root must be private to its owner.")
    runner = scratch_root.resolve(strict=True)
    candidate = runtime_root.resolve(strict=False)
    if candidate == runner or runner not in candidate.parents:
        raise ValueError("Runtime root must be a strict child of the scratch root.")
    if candidate.parent != runner:
        raise ValueError("Runtime root must be a direct child of the scratch root.")
    if runtime_root.is_symlink():
        raise ValueError("Runtime root must not be a symlink.")
    if candidate.exists():
        metadata = candidate.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Runtime root must be a directory.")
        if next(candidate.iterdir(), None) is not None:
            raise ValueError("Runtime root must be empty.")
    else:
        candidate.mkdir(mode=0o700, parents=False)
    return candidate


def _require_regular_file(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    metadata = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file.")
    return resolved


def validate_ports(ui: int, history: int, admin: int) -> Ports:
    ports = Ports(ui, history, admin)
    if len(set(ports)) != len(ports):
        raise ValueError("QA ports must be distinct.")
    if any(port < 1024 or port > 65535 for port in ports):
        raise ValueError("QA ports must be unprivileged TCP ports from 1024 through 65535.")
    return ports


def validate_ports_available(ports: Sequence[int]) -> None:
    listeners: list[socket.socket] = []
    try:
        for port in ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"QA port {port} is already in use.") from exc
    finally:
        for listener in listeners:
            listener.close()


def validate_available_memory(available_kib: int, *, minimum_mib: int) -> int:
    available_mib = available_kib // 1024
    if available_mib < minimum_mib:
        raise RuntimeError(
            f"Host has {available_mib} MiB available memory; "
            f"the matrix requires at least {minimum_mib} MiB."
        )
    return available_mib


def validate_free_disk(free_bytes: int, *, minimum_gib: int) -> int:
    free_gib = free_bytes // (1024**3)
    if free_gib < minimum_gib:
        raise RuntimeError(
            f"Host has {free_gib} GiB free disk; "
            f"the matrix requires at least {minimum_gib} GiB."
        )
    return free_gib


def _read_available_memory_kib() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise RuntimeError("MemAvailable is missing from /proc/meminfo.")


def _run(
    command: Sequence[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=True,
        capture_output=capture_output,
        text=True,
    )


def _read_app_owned_json(path: Path) -> dict[str, object]:
    result = _run(
        ["sudo", "-n", "--", "cat", str(path)],
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"app-owned JSON file was not an object: {path.name}")
    return payload


def _read_app_owned_text(path: Path) -> str:
    return _run(
        ["sudo", "-n", "--", "cat", str(path)],
        capture_output=True,
    ).stdout


def _read_app_owned_metadata(path: Path) -> tuple[int, int, int, int]:
    output = _run(
        [
            "sudo",
            "-n",
            "--",
            "stat",
            "-c",
            "%u:%g:%f:%s",
            str(path),
        ],
        capture_output=True,
    ).stdout.strip()
    fields = output.split(":")
    if len(fields) != 4:
        raise RuntimeError(f"app-owned file metadata was invalid: {path.name}")
    try:
        return int(fields[0]), int(fields[1]), int(fields[2], 16), int(fields[3])
    except ValueError as exc:
        raise RuntimeError(f"app-owned file metadata was invalid: {path.name}") from exc


def _validate_container_names_available() -> None:
    names = set(
        _run(("docker", "ps", "-a", "--format", "{{.Names}}"), capture_output=True)
        .stdout.splitlines()
    )
    collisions = sorted(names & MATRIX_CONTAINER_NAMES)
    if collisions:
        raise RuntimeError(f"Reserved matrix container names already exist: {', '.join(collisions)}")


def validate_exact_image(image: str, source_commit: str) -> None:
    if not IMAGE_PATTERN.fullmatch(image):
        raise ValueError("Image must be an exact local sha256 image ID.")
    if not SOURCE_COMMIT_PATTERN.fullmatch(source_commit):
        raise ValueError("Source commit must be a full lowercase Git commit ID.")
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{.Id}}\n{{index .Config.Labels "org.opencontainers.image.revision"}}',
            image,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Exact matrix image is not present on the local Docker host.")
    fields = result.stdout.splitlines()
    if not fields or fields[0] != image:
        raise RuntimeError("Local matrix image did not resolve to the requested image ID.")
    if len(fields) != 2 or fields[1] != source_commit:
        raise ValueError("Local matrix image source revision did not match the requested commit.")


def _compose_prefix(root: Path, variant: Variant) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        f"tjui-matrix-{variant.name}",
        "--project-directory",
        str(root),
        "-f",
        str(root / "compose.yaml"),
    ]
    for profile in variant.profiles:
        command.extend(("--profile", profile))
    return command


def _write_environment(root: Path, image: str, ports: Ports) -> None:
    environment = "\n".join(
        (
            f"JBOD_UI_IMAGE={image}",
            f"APP_UID={APP_UID}",
            f"APP_GID={APP_GID}",
            f"BACKUP_UID={BACKUP_UID}",
            f"BACKUP_GID={BACKUP_GID}",
            f"APP_PORT={ports.ui}",
            f"HISTORY_PORT={ports.history}",
            "HISTORY_BIND_ADDRESS=127.0.0.1",
            f"ADMIN_PORT={ports.admin}",
            "ADMIN_BIND_ADDRESS=127.0.0.1",
            "ADMIN_AUTO_STOP_SECONDS=0",
            "READ_UI_AUTH_MODE=basic",
            f"READ_UI_AUTH_USERNAME={AUTH_USERNAME}",
            f"READ_UI_AUTH_PASSWORD={AUTH_PASSWORD}",
            "ADMIN_AUTH_MODE=basic",
            f"ADMIN_AUTH_USERNAME={AUTH_USERNAME}",
            f"ADMIN_AUTH_PASSWORD={AUTH_PASSWORD}",
            f"APP_PUBLIC_ORIGIN=http://127.0.0.1:{ports.ui}",
            f"ADMIN_PUBLIC_ORIGIN=http://127.0.0.1:{ports.admin}",
            "METRICS_ENABLED=true",
            "SCHEDULED_BACKUP_ENABLED=false",
            "",
        )
    )
    env_path = root / ".env"
    env_path.write_text(environment, encoding="utf-8", newline="\n")
    env_path.chmod(0o600)


def _prepare_variant_root(
    root: Path,
    *,
    compose_path: Path,
    config_fixture: Path,
    image: str,
    ports: Ports,
) -> None:
    root.mkdir(mode=0o700)
    shutil.copyfile(compose_path, root / "compose.yaml")
    (root / "compose.yaml").chmod(0o600)
    _write_environment(root, image, ports)
    _run(
        (
            "sudo",
            "install",
            "-d",
            "-m",
            "0770",
            "-o",
            str(APP_UID),
            "-g",
            str(APP_GID),
            str(root / "config"),
            str(root / "config" / "ssh"),
            str(root / "config" / "tls"),
            str(root / "data"),
            str(root / "history"),
            str(root / "history" / "backups"),
            str(root / "history" / "backups" / "long-term"),
            str(root / "logs"),
        )
    )
    _run(
        (
            "sudo",
            "install",
            "-d",
            "-m",
            "0700",
            "-o",
            str(BACKUP_UID),
            "-g",
            str(BACKUP_GID),
            str(root / "backups"),
            str(root / "config" / "backup-secrets"),
        )
    )
    _run(
        (
            "sudo",
            "install",
            "-d",
            "-m",
            "2750",
            "-o",
            str(BACKUP_UID),
            "-g",
            str(APP_GID),
            str(root / "backup-status"),
        )
    )
    _run(
        (
            "sudo",
            "install",
            "-m",
            "0640",
            "-o",
            str(APP_UID),
            "-g",
            str(APP_GID),
            str(config_fixture),
            str(root / "config" / "config.yaml"),
        )
    )


def _authorization_header() -> str:
    token = base64.b64encode(f"{AUTH_USERNAME}:{AUTH_PASSWORD}".encode()).decode("ascii")
    return f"Basic {token}"


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    authenticated: bool = False,
    origin: str | None = None,
) -> tuple[int, bytes]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if authenticated:
        headers["Authorization"] = _authorization_header()
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _require_status(
    url: str,
    expected: int,
    *,
    method: str = "GET",
    payload: object | None = None,
    authenticated: bool = False,
    origin: str | None = None,
) -> bytes:
    status, body = _request(
        url,
        method=method,
        payload=payload,
        authenticated=authenticated,
        origin=origin,
    )
    if status != expected:
        raise RuntimeError(f"Unexpected HTTP status for matrix route: expected {expected}, got {status}.")
    return body


def _restart_ui(prefix: Sequence[str], ports: Ports) -> None:
    _run((*prefix, "restart", "enclosure-ui"))
    _run(
        (
            *prefix,
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "90",
            "enclosure-ui",
        )
    )
    _verify_ui(ports)


def _verify_pencil_cycle(
    root: Path,
    variant: Variant,
    ports: Ports,
    prefix: Sequence[str],
) -> None:
    url = f"http://127.0.0.1:{ports.ui}/api/sas-fabric/aliases"
    same_origin = f"http://127.0.0.1:{ports.ui}"
    object_id = f"matrix-{variant.name}"
    save_payload: dict[str, object] = {
        "object_id": object_id,
        "object_kind": "enclosure",
        "label": f"Matrix {variant.name}",
        "scope": "system",
    }
    status, _body = _request(url, method="POST", payload=save_payload, origin=same_origin)
    if status != 401:
        raise RuntimeError("anonymous pencil mutation unexpectedly succeeded")
    status, _body = _request(
        url,
        method="POST",
        payload=save_payload,
        authenticated=True,
        origin="https://cross-origin.invalid",
    )
    if status != 403:
        raise RuntimeError("cross-origin pencil mutation unexpectedly succeeded")
    response = json.loads(
        _require_status(
            url,
            200,
            method="POST",
            payload=save_payload,
            authenticated=True,
            origin=same_origin,
        )
    )
    if response.get("ok") is not True or response.get("alias", {}).get("label") != save_payload["label"]:
        raise RuntimeError("alias persistence response failed")

    alias_path = root / "data" / "sas_fabric_aliases.json"
    aliases = _read_app_owned_json(alias_path).get("sas_fabric_aliases")
    if not isinstance(aliases, dict) or len(aliases) != 1:
        raise RuntimeError("alias persistence readback failed")
    saved_alias = next(iter(aliases.values()))
    if saved_alias.get("object_id") != object_id or saved_alias.get("label") != save_payload["label"]:
        raise RuntimeError("alias persistence readback failed")
    owner_uid, owner_gid, file_mode, _size = _read_app_owned_metadata(alias_path)
    if (owner_uid, owner_gid) != (APP_UID, APP_GID) or not stat.S_ISREG(file_mode):
        raise RuntimeError("alias persistence ownership failed")

    _restart_ui(prefix, ports)
    aliases = _read_app_owned_json(alias_path).get("sas_fabric_aliases")
    if not isinstance(aliases, dict) or len(aliases) != 1:
        raise RuntimeError("alias restart persistence readback failed")
    saved_alias = next(iter(aliases.values()))
    if saved_alias.get("object_id") != object_id or saved_alias.get("label") != save_payload["label"]:
        raise RuntimeError("alias restart persistence readback failed")

    clear_payload = dict(save_payload)
    clear_payload["label"] = None
    response = json.loads(
        _require_status(
            url,
            200,
            method="POST",
            payload=clear_payload,
            authenticated=True,
            origin=same_origin,
        )
    )
    if response.get("ok") is not True or response.get("cleared") is not True:
        raise RuntimeError("alias clear response failed")
    aliases = _read_app_owned_json(alias_path).get("sas_fabric_aliases")
    if aliases != {}:
        raise RuntimeError("alias clear readback failed")


def _verify_mapping_cycle(
    root: Path,
    variant: Variant,
    ports: Ports,
    prefix: Sequence[str],
) -> None:
    base = f"http://127.0.0.1:{ports.ui}"
    same_origin = base
    export_url = f"{base}/api/mappings/export"
    initial = json.loads(_require_status(export_url, 200, authenticated=True))
    initial_revision = initial.get("revision")
    if not isinstance(initial_revision, str) or len(initial_revision) != 64:
        raise RuntimeError("initial mapping revision is unavailable")

    save_url = f"{base}/api/slots/0/mapping"
    payload = {
        "expected_revision": initial_revision,
        "notes": f"Matrix {variant.name}",
        "clear_identify_after_save": False,
    }
    response = json.loads(
        _require_status(
            save_url,
            200,
            method="POST",
            payload=payload,
            authenticated=True,
            origin=same_origin,
        )
    )
    if response.get("ok") is not True or response.get("mapping", {}).get("notes") != payload["notes"]:
        raise RuntimeError("mapping persistence response failed")
    snapshot = response.get("snapshot")
    slots = snapshot.get("slots") if isinstance(snapshot, dict) else None
    saved_slot = next(
        (
            item
            for item in slots
            if isinstance(item, dict) and item.get("slot") == 0
        ),
        None,
    ) if isinstance(slots, list) else None
    clear_revision = (
        saved_slot.get("mapping_clear_revision")
        if isinstance(saved_slot, dict)
        else None
    )
    if not isinstance(clear_revision, str) or len(clear_revision) != 64:
        raise RuntimeError("saved slot clear revision is unavailable")

    mapping_path = root / "data" / "slot_mappings.json"
    mappings = _read_app_owned_json(mapping_path).get("slot_mappings")
    if not isinstance(mappings, dict) or len(mappings) != 1:
        raise RuntimeError("mapping persistence readback failed")
    saved_mapping = next(iter(mappings.values()))
    if saved_mapping.get("slot") != 0 or saved_mapping.get("notes") != payload["notes"]:
        raise RuntimeError("mapping persistence readback failed")
    owner_uid, owner_gid, file_mode, _size = _read_app_owned_metadata(mapping_path)
    if (owner_uid, owner_gid) != (APP_UID, APP_GID) or not stat.S_ISREG(file_mode):
        raise RuntimeError("mapping persistence ownership failed")

    _restart_ui(prefix, ports)
    mappings = _read_app_owned_json(mapping_path).get("slot_mappings")
    if not isinstance(mappings, dict) or len(mappings) != 1:
        raise RuntimeError("mapping restart persistence readback failed")
    saved_mapping = next(iter(mappings.values()))
    if saved_mapping.get("slot") != 0 or saved_mapping.get("notes") != payload["notes"]:
        raise RuntimeError("mapping restart persistence readback failed")

    clear_url = f"{save_url}?{urllib.parse.urlencode({'expected_revision': clear_revision})}"
    response = json.loads(
        _require_status(
            clear_url,
            200,
            method="DELETE",
            authenticated=True,
            origin=same_origin,
        )
    )
    if response.get("ok") is not True:
        raise RuntimeError("mapping clear response failed")
    mappings = _read_app_owned_json(mapping_path).get("slot_mappings")
    if mappings != {}:
        raise RuntimeError("mapping clear readback failed")


def _verify_ui(ports: Ports) -> None:
    base = f"http://127.0.0.1:{ports.ui}"
    _require_status(f"{base}/livez", 200)
    _require_status(f"{base}/healthz", 200)
    _require_status(base, 200)


def _verify_history(ports: Ports) -> None:
    history = f"http://127.0.0.1:{ports.history}"
    _require_status(f"{history}/livez", 200)
    _require_status(f"{history}/healthz", 200)
    _require_status(
        f"http://127.0.0.1:{ports.ui}/api/history/status",
        200,
        authenticated=True,
    )


def _verify_admin(ports: Ports) -> None:
    base = f"http://127.0.0.1:{ports.admin}"
    _require_status(f"{base}/livez", 200)
    _require_status(f"{base}/healthz", 200)
    _require_status(base, 401)
    _require_status(base, 200, authenticated=True)
    state = json.loads(_require_status(f"{base}/api/admin/state", 200, authenticated=True))
    if not isinstance(state.get("runtime"), dict):
        raise RuntimeError("admin runtime state is unavailable")


def _verify_admin_initial_setup(root: Path, prefix: Sequence[str], ports: Ports) -> None:
    base = f"http://127.0.0.1:{ports.admin}"
    response = json.loads(
        _require_status(
            f"{base}/api/admin/system-setup/demo",
            200,
            method="POST",
            payload={},
            authenticated=True,
            origin=base,
        )
    )
    if response.get("ok") is not True or response.get("system", {}).get("id") != "demo-builder-lab":
        raise RuntimeError("admin-only initial setup response failed")
    config_text = _read_app_owned_text(root / "config" / "config.yaml")
    profile_text = _read_app_owned_text(root / "config" / "profiles.yaml")
    if "demo-builder-lab" not in config_text or "demo-builder-lab-chassis" not in profile_text:
        raise RuntimeError("admin-only initial setup readback failed")

    _run((*prefix, "up", "-d", "--wait", "--wait-timeout", "90", "enclosure-ui"))
    running = set(
        _run((*prefix, "ps", "--services", "--filter", "status=running"), capture_output=True)
        .stdout.splitlines()
    )
    if running != {"enclosure-admin", "enclosure-ui"}:
        raise RuntimeError("admin-only handoff started an unexpected service set")
    _verify_ui(ports)


def _safe_diagnostics(prefix: Sequence[str]) -> None:
    subprocess.run((*prefix, "ps", "--all"), check=False)
    subprocess.run(
        (*prefix, "logs", "--no-color", "--timestamps", "--tail", "200"),
        check=False,
    )


def _assert_compose_resources_removed(project: str) -> None:
    for resource_type, name in (
        *(("container", name) for name in MATRIX_CONTAINER_NAMES),
        ("network", f"{project}_default"),
    ):
        result = subprocess.run(
            ["docker", resource_type, "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode != 1:
            raise RuntimeError("Compose matrix cleanup readback found a remaining resource.")


def _compose_project_name(prefix: Sequence[str]) -> str:
    for flag in ("--project-name", "-p"):
        if flag in prefix:
            index = prefix.index(flag)
            if index + 1 < len(prefix):
                return prefix[index + 1]
    raise RuntimeError("Compose matrix project name is unavailable.")


def _cleanup_after_run(step: Callable[[], None], description: str) -> bool:
    """Run a ``finally`` cleanup step without masking the failure that got us here.

    Returns True when the step succeeded. A cleanup failure is raised only when it is
    the first failure; when another exception is already propagating the cleanup
    failure is reported on stderr and suppressed so the original error survives.
    """
    active_exception = sys.exc_info()[0] is not None
    try:
        step()
    except BaseException as cleanup_error:
        if not active_exception:
            raise
        print(
            f"warning: {description} failed and was suppressed so the original "
            f"failure propagates: {cleanup_error!r}",
            file=sys.stderr,
        )
        return False
    return True


def _remove_runtime_root(runtime_root: Path) -> None:
    cleanup = subprocess.run(
        ("sudo", "rm", "-rf", str(runtime_root)),
        check=False,
    )
    if cleanup.returncode != 0 or runtime_root.exists():
        raise RuntimeError("Compose matrix runtime-root cleanup failed.")


def _cleanup_variant(prefix: Sequence[str], root: Path) -> None:
    compose_cleanup = subprocess.run(
        (*prefix, "down", "--volumes", "--remove-orphans"),
        check=False,
    )
    scratch_cleanup = subprocess.run(
        ("sudo", "rm", "-rf", str(root)),
        check=False,
    )
    if compose_cleanup.returncode != 0 or scratch_cleanup.returncode != 0 or root.exists():
        raise RuntimeError("Compose matrix variant cleanup failed.")
    _assert_compose_resources_removed(_compose_project_name(prefix))


def _run_variant(
    runtime_root: Path,
    variant: Variant,
    *,
    compose_path: Path,
    config_fixture: Path,
    image: str,
    ports: Ports,
) -> None:
    root = runtime_root / variant.name
    _prepare_variant_root(
        root,
        compose_path=compose_path,
        config_fixture=config_fixture,
        image=image,
        ports=ports,
    )
    prefix = _compose_prefix(root, variant)
    try:
        up = [*prefix, "up", "-d", "--wait", "--wait-timeout", "90"]
        if variant.admin_initial_setup:
            up.append("--no-deps")
        up.extend(variant.services)
        _run(up)
        running = set(
            _run((*prefix, "ps", "--services", "--filter", "status=running"), capture_output=True)
            .stdout.splitlines()
        )
        if running != set(variant.services):
            raise RuntimeError(f"{variant.name} started an unexpected service set")
        if variant.ui_enabled:
            _verify_ui(ports)
            _verify_pencil_cycle(root, variant, ports, prefix)
            _verify_mapping_cycle(root, variant, ports, prefix)
        if variant.history_enabled:
            _verify_history(ports)
        if variant.admin_enabled:
            _verify_admin(ports)
        if variant.admin_initial_setup:
            _verify_admin_initial_setup(root, prefix, ports)
        print(
            json.dumps(
                {
                    "admin_initial_setup": variant.admin_initial_setup,
                    "alias_cycle": variant.ui_enabled,
                    "mapping_cycle": variant.ui_enabled,
                    "services": list(variant.services),
                    "status": "pass",
                    "variant": variant.name,
                },
                sort_keys=True,
            )
        )
    except BaseException:
        _safe_diagnostics(prefix)
        raise
    finally:
        _cleanup_after_run(
            lambda: _cleanup_variant(prefix, root),
            "compose matrix variant cleanup",
        )


def main() -> int:
    args = parse_args()
    if args.ack != "I_APPROVE_DISPOSABLE_COMPOSE_QA":
        raise RuntimeError("Explicit disposable-QA acknowledgement is required.")
    validate_exact_image(args.image, args.source_commit)
    ports = validate_ports(args.ui_port, args.history_port, args.admin_port)
    validate_ports_available(ports)
    validate_available_memory(
        _read_available_memory_kib(),
        minimum_mib=MINIMUM_AVAILABLE_MEMORY_MIB,
    )
    validate_free_disk(
        shutil.disk_usage(args.scratch_root.resolve(strict=True)).free,
        minimum_gib=MINIMUM_FREE_DISK_GIB,
    )
    _validate_container_names_available()
    compose_path = _require_regular_file(args.compose, "Compose source")
    config_fixture = _require_regular_file(args.config_fixture, "Config fixture")
    runtime_root = validate_runtime_root(args.runtime_root, args.scratch_root)
    try:
        for variant in VARIANTS:
            _run_variant(
                runtime_root,
                variant,
                compose_path=compose_path,
                config_fixture=config_fixture,
                image=args.image,
                ports=ports,
            )
    finally:
        _cleanup_after_run(
            lambda: _remove_runtime_root(runtime_root),
            "compose matrix runtime-root cleanup",
        )
    print(SUCCESS_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
