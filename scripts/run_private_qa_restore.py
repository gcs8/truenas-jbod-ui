from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import os
import selectors
import secrets
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple, Sequence

import yaml


APP_UID = 10001
APP_GID = 10001
APP_CONTAINER_NAMES = (
    "truenas-jbod-ui",
    "truenas-jbod-history",
    "truenas-jbod-admin",
    "truenas-jbod-backup",
)
APPROVAL = "I_APPROVE_PRIVATE_QA_RESTORE"
LIVE_APPROVAL = "I_APPROVE_LIVE_READ_ONLY_QA"
INSPECTION_FIELDS = {
    "ok",
    "schema_version",
    "app_version",
    "exported_at",
    "encrypted",
    "packaging",
    "selected_groups",
    "present_groups",
    "absent_groups",
    "member_count",
    "total_uncompressed_bytes",
    "aggregate_counts",
}
AGGREGATE_FIELDS = {
    "systems",
    "profiles",
    "storage_views",
    "mappings",
    "sas_fabric_aliases",
    "slot_details",
    "ssh_keys",
    "tls_files",
    "known_hosts",
    "history",
}
HISTORY_COUNT_FIELDS = {
    "tracked_slots",
    "event_count",
    "metric_sample_count",
    "metric_rollup_count",
}
REQUIRED_FULL_GROUPS = {
    "config_file",
    "runtime_overrides_file",
    "profile_file",
    "mapping_file",
    "sas_fabric_alias_file",
    "slot_detail_file",
    "history_db",
    "ssh_keys",
    "tls_trust",
    "known_hosts",
}
SENSITIVE_RECEIPT_KEYS = {
    "systems",
    "restored_paths",
    "manifest",
    "files",
    "default_system_id",
    "authorization",
    "password",
    "passphrase",
    "headers",
    "body",
    "logs",
}
LOG_COMMAND_LABEL = "docker compose logs --no-color"
RESTART_COMMAND_LABEL = "docker compose restart"
OFFLINE_NETWORK = {"internal": True}
MAX_PASSPHRASE_BYTES = 4096


class QaRestoreError(RuntimeError):
    pass


def validate_private_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise QaRestoreError(f"{label} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise QaRestoreError(f"{label} must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise QaRestoreError(f"{label} must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise QaRestoreError(f"{label} must have mode 0600")
    if metadata.st_size <= 0:
        raise QaRestoreError(f"{label} must not be empty")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise QaRestoreError(f"{label} path changed during validation")
    return resolved


def read_private_passphrase(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QaRestoreError("passphrase file must be a private regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise QaRestoreError("passphrase file must have mode 0600")
        content = os.read(descriptor, MAX_PASSPHRASE_BYTES + 1)
        if len(content) > MAX_PASSPHRASE_BYTES or os.read(descriptor, 1):
            raise QaRestoreError("passphrase file exceeds its size limit")
    finally:
        os.close(descriptor)
    try:
        passphrase = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QaRestoreError("passphrase file must contain UTF-8 text") from exc
    if passphrase.endswith("\r\n"):
        passphrase = passphrase[:-2]
    elif passphrase.endswith("\n"):
        passphrase = passphrase[:-1]
    if "\x00" in passphrase:
        raise QaRestoreError("passphrase file contains an invalid character")
    if "\r" in passphrase or "\n" in passphrase:
        raise QaRestoreError("passphrase file has ambiguous newline content")
    if not passphrase:
        raise QaRestoreError("passphrase file must contain a non-empty passphrase")
    return passphrase


def _validate_optional_count(value: object, field: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QaRestoreError(f"inspection {field} must be a non-negative integer or null")


def validate_inspection_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise QaRestoreError("inspection response must be an object")
    unexpected = set(payload) - INSPECTION_FIELDS
    missing = INSPECTION_FIELDS - set(payload)
    if unexpected or missing:
        raise QaRestoreError(
            "inspection response has unexpected fields or missing required fields"
        )
    if payload.get("ok") is not True:
        raise QaRestoreError("backup inspection did not report success")
    if payload.get("encrypted") is not True:
        raise QaRestoreError("private QA requires an encrypted FULL backup")
    aggregate = payload.get("aggregate_counts")
    if not isinstance(aggregate, dict) or set(aggregate) != AGGREGATE_FIELDS:
        raise QaRestoreError("inspection aggregate count fields are invalid")
    for key in AGGREGATE_FIELDS - {"history"}:
        _validate_optional_count(aggregate.get(key), key)
    history = aggregate.get("history")
    if history is not None:
        if not isinstance(history, dict) or set(history) != HISTORY_COUNT_FIELDS:
            raise QaRestoreError("inspection history count fields are invalid")
        for key in HISTORY_COUNT_FIELDS:
            _validate_optional_count(history.get(key), f"history.{key}")
    for key in ("selected_groups", "present_groups", "absent_groups"):
        if not isinstance(payload.get(key), list) or not all(
            isinstance(item, str) for item in payload[key]
        ):
            raise QaRestoreError(f"inspection {key} must be a string list")
    selected_groups = set(payload["selected_groups"])
    if not REQUIRED_FULL_GROUPS.issubset(selected_groups):
        raise QaRestoreError("private QA requires every default FULL backup group")
    for key in ("member_count", "total_uncompressed_bytes"):
        _validate_optional_count(payload.get(key), key)
    return payload


def reconcile_counts(
    expected: dict[str, Any],
    observed: dict[str, Any],
    *,
    prefix: str = "",
) -> None:
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        field = f"{prefix}.{key}" if prefix else key
        if expected_value is None:
            continue
        if key not in observed:
            mismatches.append(f"{field}: missing")
            continue
        observed_value = observed[key]
        if isinstance(expected_value, dict):
            if not isinstance(observed_value, dict):
                mismatches.append(
                    f"{field}: expected object, observed {type(observed_value).__name__}"
                )
                continue
            try:
                reconcile_counts(expected_value, observed_value, prefix=field)
            except QaRestoreError as exc:
                mismatches.append(str(exc))
        elif observed_value != expected_value:
            mismatches.append(
                f"{field}: expected {expected_value}, observed {observed_value}"
            )
    if mismatches:
        raise QaRestoreError("; ".join(mismatches))


def _reject_sensitive_receipt_keys(
    value: object,
    *,
    parent_key: str | None = None,
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).lower()
            aggregate_count_key = (
                parent_key == "aggregate_counts"
                and normalized_key in AGGREGATE_FIELDS
            )
            if normalized_key in SENSITIVE_RECEIPT_KEYS and not aggregate_count_key:
                raise QaRestoreError(f"sensitive receipt key: {key}")
            _reject_sensitive_receipt_keys(child, parent_key=normalized_key)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_receipt_keys(child, parent_key=parent_key)


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _reject_sensitive_receipt_keys(payload)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() and path.is_symlink():
        raise QaRestoreError("receipt path must not be a symlink")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _basic_authorization(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8"))
    return f"Basic {encoded.decode('ascii')}"


def _json_response(response: http.client.HTTPResponse, operation: str) -> dict[str, Any]:
    body = response.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise QaRestoreError(f"{operation} response exceeded the bounded JSON limit")
    if response.status < 200 or response.status >= 300:
        raise QaRestoreError(f"{operation} returned HTTP {response.status}")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QaRestoreError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise QaRestoreError(f"{operation} returned a non-object JSON payload")
    return payload


def get_json(port: int, path: str, username: str, password: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request(
            "GET",
            path,
            headers={"Authorization": _basic_authorization(username, password)},
        )
        return _json_response(connection.getresponse(), f"GET {path.split('?')[0]}")
    finally:
        connection.close()


def post_json(
    port: int,
    path: str,
    payload: dict[str, Any],
    username: str,
    password: str,
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": _basic_authorization(username, password),
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        return _json_response(connection.getresponse(), f"POST {path.split('?')[0]}")
    finally:
        connection.close()


def delete_json(port: int, path: str, username: str, password: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request(
            "DELETE",
            path,
            headers={
                "Authorization": _basic_authorization(username, password),
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        return _json_response(connection.getresponse(), f"DELETE {path.split('?')[0]}")
    finally:
        connection.close()


def post_archive(
    port: int,
    path: str,
    archive: Path,
    passphrase: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1800)
    try:
        connection.putrequest("POST", path)
        connection.putheader("Authorization", _basic_authorization(username, password))
        connection.putheader("Origin", f"http://127.0.0.1:{port}")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(archive.stat().st_size))
        connection.putheader(
            "X-Backup-Passphrase-Base64",
            base64.b64encode(passphrase.encode("utf-8")).decode("ascii"),
        )
        connection.endheaders()
        with archive.open("rb", buffering=0) as source:
            while chunk := source.read(1024 * 1024):
                connection.send(chunk)
        return _json_response(connection.getresponse(), f"POST {path.split('?')[0]}")
    finally:
        connection.close()


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


class _ServiceAccess(NamedTuple):
    host_port: int
    container_host: str
    container_port: int
    proxy_required: bool


def _resolve_service_access(
    container_name: str,
    container_port: int,
    host_port: int,
    *,
    env: dict[str, str],
) -> _ServiceAccess:
    result = subprocess.run(
        ["docker", "container", "inspect", container_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise QaRestoreError(f"unable to inspect QA container access for {container_name}")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QaRestoreError("QA container access metadata was invalid") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise QaRestoreError("QA container access metadata had an invalid shape")
    metadata = records[0]
    state = metadata.get("State")
    if not isinstance(state, dict) or state.get("Status") != "running":
        raise QaRestoreError(f"QA container is not running: {container_name}")
    network_settings = metadata.get("NetworkSettings")
    networks = network_settings.get("Networks") if isinstance(network_settings, dict) else None
    if not isinstance(networks, dict):
        raise QaRestoreError("QA container network metadata was missing")
    addresses: list[str] = []
    for entry in networks.values():
        if not isinstance(entry, dict):
            continue
        address = entry.get("IPAddress")
        if isinstance(address, str) and address:
            addresses.append(address)
    if len(addresses) != 1:
        raise QaRestoreError("QA container must have exactly one internal IPv4 address")
    try:
        container_host = str(ipaddress.IPv4Address(addresses[0]))
    except ipaddress.AddressValueError as exc:
        raise QaRestoreError("QA container must have exactly one internal IPv4 address") from exc

    ports = network_settings.get("Ports") if isinstance(network_settings, dict) else None
    if not isinstance(ports, dict):
        raise QaRestoreError("QA container port metadata was missing")
    published: list[tuple[str, str, str]] = []
    for container_key, bindings in ports.items():
        if bindings is None:
            continue
        if not isinstance(bindings, list):
            raise QaRestoreError("QA container published binding metadata was invalid")
        for binding in bindings:
            if not isinstance(binding, dict):
                raise QaRestoreError("QA container published binding metadata was invalid")
            published.append(
                (
                    str(container_key),
                    str(binding.get("HostIp") or ""),
                    str(binding.get("HostPort") or ""),
                )
            )
    expected = [(f"{container_port}/tcp", "127.0.0.1", str(host_port))]
    if published and published != expected:
        raise QaRestoreError("QA container published binding was not the exact loopback port")
    return _ServiceAccess(
        host_port=host_port,
        container_host=container_host,
        container_port=container_port,
        proxy_required=not published,
    )


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    target: tuple[str, int]


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _ProxyServer):
            return
        try:
            with socket.create_connection(server.target, timeout=5) as upstream:
                selector = selectors.DefaultSelector()
                try:
                    selector.register(self.request, selectors.EVENT_READ, upstream)
                    selector.register(upstream, selectors.EVENT_READ, self.request)
                    while True:
                        for key, _ in selector.select(timeout=60):
                            source_socket = key.fileobj
                            target_socket = key.data
                            if not isinstance(source_socket, socket.socket) or not isinstance(
                                target_socket, socket.socket
                            ):
                                return
                            data = source_socket.recv(65536)
                            if not data:
                                return
                            target_socket.sendall(data)
                finally:
                    selector.close()
        except OSError:
            return


class _LoopbackProxySet:
    def __init__(self, bindings: Sequence[tuple[int, str, int]]) -> None:
        self._bindings = list(bindings)
        self._servers: list[tuple[_ProxyServer, threading.Thread]] = []

    def start(self) -> None:
        if self._servers:
            raise QaRestoreError("loopback service access is already started")
        try:
            for host_port, container_host, container_port in self._bindings:
                server = _ProxyServer(("127.0.0.1", host_port), _ProxyHandler)
                server.target = (container_host, container_port)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self._servers.append((server, thread))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        servers, self._servers = self._servers, []
        for server, _ in servers:
            server.shutdown()
            server.server_close()
        for _, thread in servers:
            thread.join(timeout=5)
            if thread.is_alive():
                raise QaRestoreError("loopback service access thread did not stop")
        occupied = [
            host_port
            for host_port, _, _ in self._bindings
            if _port_is_listening(host_port)
        ]
        if occupied:
            raise QaRestoreError("loopback service access ports were not released")


def _available_memory_kib() -> int:
    fields: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, raw_value = line.partition(":")
        if key in {"MemAvailable", "SwapFree"}:
            fields[key] = int(raw_value.strip().split()[0])
    return fields.get("MemAvailable", 0) + fields.get("SwapFree", 0)


def _validate_runtime_preflight(
    scratch_root: Path,
    runtime_root: Path,
    ports: Sequence[int],
    minimum_available_memory_mib: int,
    minimum_free_disk_gib: int,
) -> None:
    if not scratch_root.is_absolute() or not runtime_root.is_absolute():
        raise QaRestoreError("scratch and runtime roots must be absolute paths")
    scratch = scratch_root.resolve(strict=True)
    if scratch_root.is_symlink() or not scratch.is_dir():
        raise QaRestoreError("scratch root must be a real directory")
    if stat.S_IMODE(scratch.stat().st_mode) & 0o077:
        raise QaRestoreError("scratch root must not allow group or other access")
    if runtime_root.exists() or runtime_root.is_symlink():
        raise QaRestoreError("runtime root must not already exist")
    if runtime_root.parent.resolve(strict=True) != scratch:
        raise QaRestoreError("runtime root must be a direct child of the scratch root")
    if len(set(ports)) != len(ports) or any(port < 1024 or port > 65535 for port in ports):
        raise QaRestoreError("QA ports must be distinct unprivileged ports")
    occupied = [port for port in ports if not _port_is_free(port)]
    if occupied:
        raise QaRestoreError(f"QA ports are already occupied: {occupied}")
    available_mib = _available_memory_kib() // 1024
    if available_mib < minimum_available_memory_mib:
        raise QaRestoreError(
            f"QA host has {available_mib} MiB available; "
            f"{minimum_available_memory_mib} MiB is required"
        )
    free_gib = shutil.disk_usage(scratch).free // (1024**3)
    if free_gib < minimum_free_disk_gib:
        raise QaRestoreError(
            f"QA scratch has {free_gib} GiB free; {minimum_free_disk_gib} GiB is required"
        )


def _compose_child_environment(image: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "APP_BIND_ADDRESS": "127.0.0.1",
        "JBOD_UI_IMAGE": image,
    }


def _validate_container_names_available(*, env: dict[str, str]) -> None:
    for name in APP_CONTAINER_NAMES:
        result = subprocess.run(
            ["docker", "container", "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            raise QaRestoreError(f"fixed QA container name is already in use: {name}")
        if result.returncode != 1:
            raise QaRestoreError(
                "unable to prove fixed QA container names are available"
            )


def _validate_exact_image(
    image: str,
    source_commit: str,
    *,
    env: dict[str, str],
) -> None:
    if (
        not image.startswith("sha256:")
        or len(image) != 71
        or any(character not in "0123456789abcdef" for character in image[7:])
    ):
        raise QaRestoreError("image must be an exact lowercase sha256 image ID")
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .}}", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise QaRestoreError("exact QA image is not present on the local Docker host")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QaRestoreError("local Docker image metadata was invalid") from exc
    if not isinstance(metadata, dict) or metadata.get("Id") != image:
        raise QaRestoreError("local Docker image did not resolve to the exact requested ID")
    config = metadata.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    revision = (
        labels.get("org.opencontainers.image.revision")
        if isinstance(labels, dict)
        else None
    )
    if revision != source_commit:
        raise QaRestoreError("local Docker image source revision does not match --source-commit")


def _validate_exact_source(repo_root: Path, source_commit: str) -> None:
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise QaRestoreError("source commit must be a full lowercase Git commit ID")
    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
    )
    if head.returncode != 0 or status.returncode != 0:
        raise QaRestoreError("unable to verify the local QA source checkout")
    if head.stdout.strip() != source_commit:
        raise QaRestoreError("local QA source HEAD does not match --source-commit")
    if status.stdout:
        raise QaRestoreError("local QA source checkout is not clean")


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path.parent.chmod(0o700)
    with log_path.open("ab", buffering=0) as output:
        os.chmod(log_path, 0o600)
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    if result.returncode != 0:
        raise QaRestoreError(f"command failed with exit code {result.returncode}")


def _app_owned_reader(command: Sequence[str]) -> str:
    result = subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise QaRestoreError("app-owned QA state could not be read")
    return result.stdout


def _read_app_owned_json(path: Path) -> dict[str, Any]:
    payload = json.loads(
        _app_owned_reader(["sudo", "-n", "--", "cat", str(path)])
    )
    if not isinstance(payload, dict):
        raise QaRestoreError("app-owned QA JSON must be an object")
    return payload


def _app_owned_file_exists(path: Path) -> bool:
    result = subprocess.run(
        ["sudo", "-n", "--", "test", "-f", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise QaRestoreError("app-owned QA file existence check failed")
    return result.returncode == 0


def _count_app_owned_files(
    root: Path,
    *,
    exclude_paths: set[Path] | None = None,
) -> int:
    code = (
        "import json,os,stat,sys;"
        "root=sys.argv[1];excluded=set(json.loads(sys.argv[2]));count=0;"
        "\nfor parent,dirs,files in os.walk(root,followlinks=False):"
        "\n dirs[:]=[name for name in dirs if not os.path.islink(os.path.join(parent,name))]"
        "\n for name in files:"
        "\n  path=os.path.join(parent,name)"
        "\n  try: mode=os.lstat(path).st_mode"
        "\n  except OSError: continue"
        "\n  count += int(stat.S_ISREG(mode) and path not in excluded)"
        "\nprint(count)"
    )
    excluded = sorted(str(path) for path in (exclude_paths or set()))
    output = _app_owned_reader(
        [
            "sudo",
            "-n",
            "--",
            "python3",
            "-c",
            code,
            str(root),
            json.dumps(excluded, separators=(",", ":")),
        ]
    )
    try:
        count = int(output.strip())
    except ValueError as exc:
        raise QaRestoreError("app-owned QA directory count was invalid") from exc
    if count < 0:
        raise QaRestoreError("app-owned QA directory count was negative")
    return count


def _remove_runtime_root(runtime_root: Path) -> None:
    cleanup = subprocess.run(
        ["sudo", "rm", "-rf", str(runtime_root)],
        check=False,
    )
    if cleanup.returncode != 0 or runtime_root.exists():
        raise QaRestoreError("private QA runtime cleanup failed")


def _assert_compose_resources_removed(
    project: str,
    *,
    env: dict[str, str],
) -> None:
    targets = [
        *(
            ("container", name)
            for name in APP_CONTAINER_NAMES
        ),
        ("network", f"{project}_default"),
    ]
    for resource_type, name in targets:
        result = subprocess.run(
            ["docker", resource_type, "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode != 1:
            raise QaRestoreError("private QA cleanup readback found a remaining resource")


def _compose_command(runtime_root: Path, project: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(runtime_root / "docker-compose.yml"),
        "-f",
        str(runtime_root / "qa-restore.override.yml"),
        "--profile",
        "history",
        "--profile",
        "admin",
    ]


def _write_runtime_files(
    repo_root: Path,
    runtime_root: Path,
    image: str,
    ports: tuple[int, int, int],
    username: str,
    password: str,
    *,
    live_read_only: bool,
) -> None:
    runtime_root.mkdir(mode=0o700)
    for name in (
        "config",
        "config/ssh",
        "config/tls",
        "data",
        "history",
        "logs",
        "backups",
        "backup-status",
    ):
        (runtime_root / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_root / "docker-compose.yml", runtime_root / "docker-compose.yml")
    shutil.copy2(
        repo_root / "tests" / "fixtures" / "ci-smoke-config.yaml",
        runtime_root / "config" / "config.yaml",
    )
    environment = "\n".join(
        (
            f"JBOD_UI_IMAGE={image}",
            "APP_BIND_ADDRESS=127.0.0.1",
            f"APP_PORT={ports[0]}",
            f"HISTORY_PORT={ports[1]}",
            f"ADMIN_PORT={ports[2]}",
            "HISTORY_BIND_ADDRESS=127.0.0.1",
            "ADMIN_BIND_ADDRESS=127.0.0.1",
            f"APP_PUBLIC_ORIGIN=http://127.0.0.1:{ports[0]}",
            "READ_UI_AUTH_MODE=basic",
            f"READ_UI_AUTH_USERNAME={username}",
            f"READ_UI_AUTH_PASSWORD={password}",
            "ADMIN_AUTH_MODE=basic",
            f"ADMIN_AUTH_USERNAME={username}",
            f"ADMIN_AUTH_PASSWORD={password}",
            f"ADMIN_PUBLIC_ORIGIN=http://127.0.0.1:{ports[2]}",
            "APP_LOG_LEVEL=INFO",
            "LOG_FORMAT=json",
            "METRICS_ENABLED=true",
            "HISTORY_STARTUP_GRACE_SECONDS=0",
            "HISTORY_POLL_INTERVAL_SECONDS=3600",
            "ADMIN_AUTO_STOP_SECONDS=0",
            "",
        )
    )
    env_path = runtime_root / ".env"
    env_path.write_text(environment, encoding="utf-8")
    env_path.chmod(0o600)
    override = {
        "networks": {
            "default": (
                {"internal": False}
                if live_read_only
                else dict(OFFLINE_NETWORK)
            ),
        },
    }
    override_path = runtime_root / "qa-restore.override.yml"
    override_path.write_text(
        "networks:\n  default:\n    internal: "
        + ("false\n" if live_read_only else "true\n"),
        encoding="utf-8",
    )
    override_path.chmod(0o600)
    if override["networks"]["default"] != {"internal": not live_read_only}:
        raise QaRestoreError("QA network override construction failed")


def _wait_json(
    port: int,
    path: str,
    username: str,
    password: str,
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return get_json(port, path, username, password)
        except (OSError, QaRestoreError) as exc:
            last_error = exc
            time.sleep(1)
    raise QaRestoreError(f"health wait expired after {timeout_seconds}s") from last_error


def _wait_history_idle(
    port: int,
    username: str,
    password: str,
    *,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = get_json(
            port,
            "/api/history/overview?exact_counts=true",
            username,
            password,
        )
        collector = payload.get("collector")
        if not isinstance(collector, dict):
            raise QaRestoreError("history overview omitted collector state")
        collection_running = collector.get("collection_running")
        if collection_running is False:
            return payload
        if collection_running is not True:
            raise QaRestoreError("history overview omitted collector state")
        time.sleep(1)
    raise QaRestoreError(
        f"history collector remained active after {timeout_seconds}s"
    )


def _json_count(path: Path, key: str) -> int:
    if not _app_owned_file_exists(path):
        return 0
    payload = _read_app_owned_json(path)
    entries = payload.get(key)
    return len(entries) if isinstance(entries, dict) else 0


def _validated_history_counts(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in HISTORY_COUNT_FIELDS:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QaRestoreError(f"history count {key} is missing or invalid")
        counts[key] = value
    return counts


def _qa_host_path(runtime_root: Path, configured_path: object) -> Path:
    if not isinstance(configured_path, str):
        raise QaRestoreError("restored state path must be a string")
    candidate = PurePosixPath(configured_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise QaRestoreError("restored state path is outside QA mounts")
    for container_root, host_name in (
        (PurePosixPath("/app/config"), "config"),
        (PurePosixPath("/app/data"), "data"),
        (PurePosixPath("/app/history"), "history"),
        (PurePosixPath("/app/logs"), "logs"),
    ):
        try:
            relative = candidate.relative_to(container_root)
        except ValueError:
            continue
        return runtime_root / host_name / Path(*relative.parts)
    raise QaRestoreError("restored state path is outside QA mounts")


def _runtime_state_paths(runtime_root: Path) -> dict[str, Path]:
    config_path = runtime_root / "config" / "config.yaml"
    payload = yaml.safe_load(
        _app_owned_reader(["sudo", "-n", "--", "cat", str(config_path)])
    ) or {}
    if not isinstance(payload, dict):
        raise QaRestoreError("restored config must be a mapping")
    paths = payload.get("paths") or {}
    if not isinstance(paths, dict):
        raise QaRestoreError("restored path settings must be mappings")
    configured = {
        "mapping_file": paths.get("mapping_file", "/app/data/slot_mappings.json"),
        "sas_fabric_alias_file": paths.get(
            "sas_fabric_alias_file",
            "/app/data/sas_fabric_aliases.json",
        ),
        "slot_detail_cache_file": paths.get(
            "slot_detail_cache_file",
            "/app/data/slot_detail_cache.json",
        ),
        "known_hosts_path": "/app/data/known_hosts",
    }
    return {
        key: _qa_host_path(runtime_root, value)
        for key, value in configured.items()
    }


def _observed_counts(
    runtime_root: Path,
    ports: tuple[int, int, int],
    username: str,
    password: str,
) -> tuple[dict[str, Any], str]:
    admin_state = get_json(ports[2], "/api/admin/state", username, password)
    systems = admin_state.get("systems")
    profiles = admin_state.get("profiles")
    if not isinstance(systems, list) or not isinstance(profiles, list):
        raise QaRestoreError("admin state did not contain systems and profiles lists")
    system_id = ""
    storage_views = 0
    for system in systems:
        if not isinstance(system, dict):
            raise QaRestoreError("admin state system entry was invalid")
        if not system_id and isinstance(system.get("id"), str):
            system_id = system["id"]
        views = system.get("storage_views")
        storage_views += len(views) if isinstance(views, list) else 0
    if not system_id:
        raise QaRestoreError("restored configuration did not expose a system")
    history = get_json(
        ports[1],
        "/api/history/overview?exact_counts=true",
        username,
        password,
    )
    history_counts = history.get("counts")
    if not isinstance(history_counts, dict):
        raise QaRestoreError("history overview did not contain exact counts")
    state_paths = _runtime_state_paths(runtime_root)
    return (
        {
            "systems": len(systems),
            "profiles": sum(
                1
                for profile in profiles
                if isinstance(profile, dict) and profile.get("is_custom") is True
            ),
            "storage_views": storage_views,
            "mappings": _json_count(state_paths["mapping_file"], "slot_mappings"),
            "sas_fabric_aliases": _json_count(
                state_paths["sas_fabric_alias_file"],
                "sas_fabric_aliases",
            ),
            "slot_details": _json_count(
                state_paths["slot_detail_cache_file"],
                "slot_details",
            ),
            "ssh_keys": _count_app_owned_files(
                runtime_root / "config" / "ssh",
                exclude_paths={state_paths["known_hosts_path"]},
            ),
            "tls_files": _count_app_owned_files(runtime_root / "config" / "tls"),
            "known_hosts": int(_app_owned_file_exists(state_paths["known_hosts_path"])),
            "history": _validated_history_counts(history_counts),
        },
        system_id,
    )


def _exercise_pencil_writes(
    runtime_root: Path,
    ui_port: int,
    username: str,
    password: str,
    system_id: str,
    *,
    live_read_only: bool,
) -> dict[str, bool]:
    nonce = uuid.uuid4().hex
    alias_payload = {
        "object_kind": "system",
        "object_id": f"qa-restore-{nonce}",
        "label": "QA restore transient label",
    }
    saved_alias = post_json(
        ui_port,
        "/api/sas-fabric/aliases",
        alias_payload,
        username,
        password,
    )
    saved_alias_record = saved_alias.get("alias")
    if (
        saved_alias.get("ok") is not True
        or not isinstance(saved_alias_record, dict)
        or saved_alias_record.get("label") != alias_payload["label"]
    ):
        raise QaRestoreError("SAS Fabric label save/readback failed")
    cleared_alias = post_json(
        ui_port,
        "/api/sas-fabric/aliases",
        {**alias_payload, "label": None},
        username,
        password,
    )
    if (
        cleared_alias.get("ok") is not True
        or cleared_alias.get("cleared") is not True
        or cleared_alias.get("alias") is not None
    ):
        raise QaRestoreError("SAS Fabric label cleanup failed")

    if not live_read_only:
        return {"sas_fabric_label": True, "slot_mapping": False}

    inventory = get_json(
        ui_port,
        "/api/inventory?force=true&"
        + urllib.parse.urlencode({"system_id": system_id}),
        username,
        password,
    )
    enclosure_id = inventory.get("selected_enclosure_id")
    if not isinstance(enclosure_id, str) or not enclosure_id:
        raise QaRestoreError("live inventory did not expose a selected enclosure")
    query = urllib.parse.urlencode(
        {"system_id": system_id, "enclosure_id": enclosure_id}
    )
    export = get_json(
        ui_port,
        f"/api/mappings/export?{query}",
        username,
        password,
    )
    revision = export.get("revision")
    if not isinstance(revision, str):
        raise QaRestoreError("mapping export did not return a revision")
    mapping_payload = {
        "expected_revision": revision,
        "notes": f"QA restore transient mapping {nonce}",
        "clear_identify_after_save": False,
    }
    saved_mapping = post_json(
        ui_port,
        f"/api/slots/0/mapping?{query}",
        mapping_payload,
        username,
        password,
    )
    saved_mapping_record = saved_mapping.get("mapping")
    if (
        saved_mapping.get("ok") is not True
        or not isinstance(saved_mapping_record, dict)
        or saved_mapping_record.get("notes") != mapping_payload["notes"]
    ):
        raise QaRestoreError("slot mapping save/readback failed")
    saved_snapshot = saved_mapping.get("snapshot")
    saved_slots = (
        saved_snapshot.get("slots")
        if isinstance(saved_snapshot, dict)
        else None
    )
    saved_slot = next(
        (
            item
            for item in saved_slots
            if isinstance(item, dict) and item.get("slot") == 0
        ),
        None,
    ) if isinstance(saved_slots, list) else None
    next_revision = (
        saved_slot.get("mapping_clear_revision")
        if isinstance(saved_slot, dict)
        else None
    )
    if not isinstance(next_revision, str):
        raise QaRestoreError("saved slot did not return a clear revision")
    removed = delete_json(
        ui_port,
        "/api/slots/0/mapping?"
        + urllib.parse.urlencode(
            {
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "expected_revision": next_revision,
            }
        ),
        username,
        password,
    )
    if removed.get("ok") is not True:
        raise QaRestoreError("slot mapping cleanup failed")
    return {"sas_fabric_label": True, "slot_mapping": True}


def _safe_import_summary(
    payload: dict[str, Any],
    *,
    expected_groups: Sequence[str],
    expected_absent_groups: Sequence[str] = (),
) -> dict[str, Any]:
    failures = payload.get("restart_failures")
    if not isinstance(failures, dict) or failures:
        raise QaRestoreError("backup import restart failures were missing or nonempty")
    if payload.get("ok") is not True:
        raise QaRestoreError("backup import did not complete with clean service restart")
    expected_restart_keys = {"ui", "history"}
    stopped = payload.get("stopped_containers")
    restarted = payload.get("restarted_containers")
    if (
        not isinstance(stopped, list)
        or set(stopped) != expected_restart_keys
        or len(stopped) != len(expected_restart_keys)
    ):
        raise QaRestoreError("backup import stopped containers did not match the request")
    if (
        not isinstance(restarted, list)
        or set(restarted) != expected_restart_keys
        or len(restarted) != len(expected_restart_keys)
    ):
        raise QaRestoreError("backup import restarted containers did not match the request")
    included_groups = payload.get("included_groups")
    if (
        not isinstance(included_groups, list)
        or set(included_groups) != set(expected_groups)
        or len(included_groups) != len(set(included_groups))
    ):
        raise QaRestoreError("backup import group set did not match inspection")
    preserved_absent = payload.get("preserved_absent_groups")
    if (
        not isinstance(preserved_absent, list)
        or set(preserved_absent) != set(expected_absent_groups)
        or len(preserved_absent) != len(set(preserved_absent))
    ):
        raise QaRestoreError("backup import preserved absent groups did not match inspection")
    if payload.get("restored_history_database") is not True:
        raise QaRestoreError("backup import did not restore the history database")
    systems = payload.get("systems")
    restored_paths = payload.get("restored_paths")
    return {
        "ok": True,
        "schema_version": payload.get("schema_version"),
        "app_version": payload.get("app_version"),
        "encrypted": payload.get("encrypted"),
        "packaging": payload.get("packaging"),
        "system_count": payload.get("system_count"),
        "system_metadata_count": len(systems) if isinstance(systems, list) else None,
        "included_groups": payload.get("included_groups"),
        "restored_history_database": payload.get("restored_history_database"),
        "restored_path_count": len(restored_paths) if isinstance(restored_paths, list) else None,
        "stopped_container_count": len(stopped),
        "restarted_container_count": len(restarted),
        "restart_failure_count": 0,
    }


def _capture_compose_logs(
    compose: Sequence[str],
    runtime_root: Path,
    raw_dir: Path,
    *,
    env: dict[str, str],
) -> None:
    log_path = raw_dir / "compose.log"
    with log_path.open("wb") as output:
        os.chmod(log_path, 0o600)
        subprocess.run(
            [*compose, "logs", "--no-color"],
            cwd=runtime_root,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
            env=env,
        )
    if LOG_COMMAND_LABEL != "docker compose logs --no-color":
        raise QaRestoreError("compose log command label drifted")


def _run_browser_and_perf(
    repo_root: Path,
    ports: tuple[int, int, int],
    username: str,
    password: str,
    raw_dir: Path,
    *,
    live_read_only: bool,
) -> dict[str, bool]:
    username_file = raw_dir / ".qa-http-username"
    password_file = raw_dir / ".qa-http-password"
    private_output_dir = raw_dir / "playwright-private"
    private_output_dir.mkdir(mode=0o700)
    created_credentials: list[Path] = []
    try:
        for path, value in (
            (username_file, username),
            (password_file, password),
        ):
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            created_credentials.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
        env = {
            key: os.environ[key]
            for key in (
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "TZ",
                "CI",
                "PLAYWRIGHT_BROWSER_CHANNEL",
            )
            if key in os.environ
        }
        env.update(
            {
                "PLAYWRIGHT_BASE_URL": f"http://127.0.0.1:{ports[0]}",
                "PLAYWRIGHT_ADMIN_BASE_URL": f"http://127.0.0.1:{ports[2]}",
                "PLAYWRIGHT_HTTP_USERNAME_FILE": str(username_file.resolve()),
                "PLAYWRIGHT_HTTP_PASSWORD_FILE": str(password_file.resolve()),
                "PLAYWRIGHT_PRIVATE_OUTPUT_DIR": str(private_output_dir.resolve()),
                "PYTHON": sys.executable,
            }
        )
        _run(
            [
                "npx",
                "playwright",
                "test",
                "qa/private-restore.spec.js",
                "qa/admin-operations.spec.js",
            ],
            cwd=repo_root,
            log_path=raw_dir / "browser-offline.log",
            timeout=900,
            env=env,
        )
        _run(
            [
                sys.executable,
                "scripts/run_history_perf_harness.py",
                "--base-url",
                f"http://127.0.0.1:{ports[1]}",
                "--include-exact-counts",
                "--iterations",
                "1",
                "--no-record",
                "--output",
                str(raw_dir / "history-perf.json"),
            ],
            cwd=repo_root,
            log_path=raw_dir / "history-perf.log",
            timeout=900,
        )
        results = {"offline_browser": True, "history_performance": True}
        if live_read_only:
            env["PLAYWRIGHT_LIVE_APPLIANCE_QA"] = "1"
            _run(
                [
                    "npx",
                    "playwright",
                    "test",
                    "qa/ui-switching.spec.js",
                    "qa/esxi-smoke.spec.js",
                ],
                cwd=repo_root,
                log_path=raw_dir / "browser-live-read-only.log",
                timeout=1800,
                env=env,
            )
            _run(
                [
                    sys.executable,
                    "scripts/run_perf_harness.py",
                    "--base-url",
                    f"http://127.0.0.1:{ports[0]}",
                    "--username-file",
                    str(username_file),
                    "--password-file",
                    str(password_file),
                    "--iterations",
                    "1",
                    "--skip-mappings-import-roundtrip",
                    "--no-record",
                    "--output",
                    str(raw_dir / "app-perf.json"),
                ],
                cwd=repo_root,
                log_path=raw_dir / "app-perf.log",
                timeout=1800,
                env=env,
            )
            results.update({"live_browser": True, "app_performance": True})
        return results
    finally:
        for path in created_credentials:
            path.unlink(missing_ok=True)


def _validate_target_handle(value: str) -> str:
    prefix = "run-"
    suffix = value[len(prefix) :] if value.startswith(prefix) else ""
    if len(suffix) != 32 or any(
        character not in "0123456789abcdef" for character in suffix
    ):
        raise QaRestoreError(
            "target handle must use the opaque run-<32 lowercase hex> form"
        )
    return value


def _validate_mandatory_gates(*, skip_browser_and_performance: bool) -> None:
    if skip_browser_and_performance:
        raise QaRestoreError("browser and performance gates are mandatory for PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a private production-derived restore drill on a disposable Docker QA host."
    )
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--passphrase-file", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target-handle", required=True)
    parser.add_argument("--scratch-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--app-port", type=int, default=28080)
    parser.add_argument("--history-port", type=int, default=28081)
    parser.add_argument("--admin-port", type=int, default=28082)
    parser.add_argument("--minimum-available-memory-mib", type=int, default=3072)
    parser.add_argument("--minimum-free-disk-gib", type=int, default=10)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--live-approval")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--skip-browser-and-performance", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.approval != APPROVAL:
        raise QaRestoreError("private QA restore approval phrase did not match")
    if args.live_read_only and args.live_approval != LIVE_APPROVAL:
        raise QaRestoreError("live read-only QA requires its separate approval phrase")
    _validate_mandatory_gates(
        skip_browser_and_performance=args.skip_browser_and_performance
    )
    target_handle = _validate_target_handle(args.target_handle)

    repo_root = Path(__file__).resolve().parents[1]
    _validate_exact_source(repo_root, args.source_commit)
    compose_env = _compose_child_environment(args.image)
    _validate_exact_image(args.image, args.source_commit, env=compose_env)
    backup = validate_private_file(args.backup, "backup")
    passphrase_path = validate_private_file(args.passphrase_file, "passphrase file")
    passphrase = read_private_passphrase(passphrase_path)
    ports = (args.app_port, args.history_port, args.admin_port)
    _validate_runtime_preflight(
        args.scratch_root,
        args.runtime_root,
        ports,
        args.minimum_available_memory_mib,
        args.minimum_free_disk_gib,
    )
    _validate_container_names_available(env=compose_env)

    evidence_dir = args.evidence_dir
    if evidence_dir.exists() or evidence_dir.is_symlink():
        raise QaRestoreError("evidence directory must not already exist")
    raw_dir = evidence_dir / "raw-private"
    raw_dir.mkdir(parents=True, mode=0o700)
    evidence_dir.chmod(0o700)
    raw_dir.chmod(0o700)
    run_id = f"private-qa-{uuid.uuid4().hex}"
    username = f"qa-{secrets.token_hex(8)}"
    password = secrets.token_urlsafe(32)
    project = f"tjuiqa{uuid.uuid4().hex[:12]}"
    compose: list[str] = []
    service_access: _LoopbackProxySet | None = None
    stack_started = False
    completed = False
    phase = "preflight"
    started_at = time.time()
    receipt_path = evidence_dir / "sanitized-receipt.json"
    try:
        phase = "runtime-setup"
        _write_runtime_files(
            repo_root,
            args.runtime_root,
            args.image,
            ports,
            username,
            password,
            live_read_only=args.live_read_only,
        )
        _run(
            [
                "sudo",
                sys.executable,
                str(repo_root / "scripts" / "prepare_nonroot_bind_mounts.py"),
                str(args.runtime_root),
                "--uid",
                str(APP_UID),
                "--gid",
                str(APP_GID),
                "--apply",
            ],
            cwd=repo_root,
            log_path=raw_dir / "ownership-preflight.log",
            timeout=300,
        )
        compose = _compose_command(args.runtime_root, project)
        phase = "compose-start"
        _run(
            [*compose, "up", "-d", "enclosure-ui", "enclosure-history", "enclosure-admin"],
            cwd=args.runtime_root,
            log_path=raw_dir / "compose-up.log",
            timeout=600,
            env=compose_env,
        )
        stack_started = True
        access_plans = (
            _resolve_service_access(
                "truenas-jbod-ui", 8000, ports[0], env=compose_env
            ),
            _resolve_service_access(
                "truenas-jbod-history", 8001, ports[1], env=compose_env
            ),
            _resolve_service_access(
                "truenas-jbod-admin", 8002, ports[2], env=compose_env
            ),
        )
        service_access = _LoopbackProxySet(
            [
                (access.host_port, access.container_host, access.container_port)
                for access in access_plans
                if access.proxy_required
            ]
        )
        service_access.start()
        _wait_json(ports[0], "/livez", username, password)
        _wait_json(ports[1], "/livez", username, password)
        _wait_json(ports[2], "/livez", username, password)

        phase = "backup-inspection"
        inspection = validate_inspection_payload(
            post_archive(
                ports[2],
                "/api/admin/backup/inspect",
                backup,
                passphrase,
                username,
                password,
            )
        )
        phase = "backup-import"
        imported = post_archive(
            ports[2],
            "/api/admin/backup/import?stop_services=true&restart_services=true",
            backup,
            passphrase,
            username,
            password,
        )
        import_summary = _safe_import_summary(
            imported,
            expected_groups=inspection["selected_groups"],
            expected_absent_groups=inspection["absent_groups"],
        )
        _wait_json(ports[0], "/healthz", username, password)
        _wait_json(ports[1], "/healthz", username, password)
        _wait_json(ports[2], "/healthz", username, password)
        _wait_history_idle(ports[1], username, password)

        phase = "aggregate-reconcile"
        observed, system_id = _observed_counts(
            args.runtime_root, ports, username, password
        )
        reconcile_counts(inspection["aggregate_counts"], observed)

        phase = "pencil-writes"
        pencil_results = _exercise_pencil_writes(
            args.runtime_root,
            ports[0],
            username,
            password,
            system_id,
            live_read_only=args.live_read_only,
        )
        observed_after_writes, _ = _observed_counts(
            args.runtime_root, ports, username, password
        )
        reconcile_counts(inspection["aggregate_counts"], observed_after_writes)

        phase = "restart-survival"
        if RESTART_COMMAND_LABEL != "docker compose restart":
            raise QaRestoreError("compose restart command label drifted")
        if service_access is None:
            raise QaRestoreError("loopback service access was not initialized")
        service_access.close()
        service_access = None
        _run(
            [*compose, "restart"],
            cwd=args.runtime_root,
            log_path=raw_dir / "compose-restart.log",
            timeout=600,
            env=compose_env,
        )
        access_plans = (
            _resolve_service_access(
                "truenas-jbod-ui", 8000, ports[0], env=compose_env
            ),
            _resolve_service_access(
                "truenas-jbod-history", 8001, ports[1], env=compose_env
            ),
            _resolve_service_access(
                "truenas-jbod-admin", 8002, ports[2], env=compose_env
            ),
        )
        service_access = _LoopbackProxySet(
            [
                (access.host_port, access.container_host, access.container_port)
                for access in access_plans
                if access.proxy_required
            ]
        )
        service_access.start()
        _wait_json(ports[0], "/healthz", username, password)
        _wait_json(ports[1], "/healthz", username, password)
        _wait_json(ports[2], "/healthz", username, password)
        _wait_history_idle(ports[1], username, password)
        observed_after_restart, _ = _observed_counts(
            args.runtime_root, ports, username, password
        )
        reconcile_counts(inspection["aggregate_counts"], observed_after_restart)

        qa_results: dict[str, bool] = {}
        if not args.skip_browser_and_performance:
            phase = "browser-and-performance"
            qa_results = _run_browser_and_perf(
                repo_root,
                ports,
                username,
                password,
                raw_dir,
                live_read_only=args.live_read_only,
            )

        phase = "evidence"
        _capture_compose_logs(
            compose,
            args.runtime_root,
            raw_dir,
            env=compose_env,
        )
        if not args.keep_running:
            phase = "cleanup"
            if service_access is not None:
                service_access.close()
                service_access = None
            _run(
                [*compose, "down", "--remove-orphans", "--volumes"],
                cwd=args.runtime_root,
                log_path=raw_dir / "compose-down.log",
                timeout=600,
                env=compose_env,
            )
            _assert_compose_resources_removed(project, env=compose_env)
            stack_started = False
            _remove_runtime_root(args.runtime_root)
        receipt = {
            "status": "PASS",
            "run_id": run_id,
            "source_commit": args.source_commit,
            "image_id": args.image,
            "backup_sha256": sha256_file(backup),
            "backup_size_bytes": backup.stat().st_size,
            "target_handle": target_handle,
            "network_mode": "live-read-only" if args.live_read_only else "egress-blocked",
            "inspection": {
                "schema_version": inspection["schema_version"],
                "app_version": inspection["app_version"],
                "encrypted": inspection["encrypted"],
                "packaging": inspection["packaging"],
                "selected_groups": inspection["selected_groups"],
                "present_groups": inspection["present_groups"],
                "absent_groups": inspection["absent_groups"],
                "member_count": inspection["member_count"],
                "total_uncompressed_bytes": inspection["total_uncompressed_bytes"],
                "aggregate_counts": inspection["aggregate_counts"],
            },
            "import_summary": import_summary,
            "aggregate_counts_match": True,
            "pencil_cycles": {
                **pencil_results,
                "cleanup_verified": True,
            },
            "restart_survival": True,
            "qa_results": qa_results,
            "stack_running": bool(args.keep_running),
            "elapsed_seconds": round(time.time() - started_at, 3),
        }
        write_private_json(receipt_path, receipt)
        completed = True
        print(
            "private_qa_restore=PASS "
            f"receipt={receipt_path} stack_running={str(args.keep_running).lower()}"
        )
        return 0
    except BaseException as exc:
        if stack_started and compose:
            try:
                _capture_compose_logs(
                    compose,
                    args.runtime_root,
                    raw_dir,
                    env=compose_env,
                )
            except BaseException:
                pass
        failure_receipt = {
            "status": "FAIL",
            "run_id": run_id,
            "source_commit": args.source_commit,
            "image_id": args.image,
            "backup_sha256": sha256_file(backup),
            "target_handle": target_handle,
            "failed_phase": phase,
            "error_class": type(exc).__name__,
            "stack_running": False,
            "elapsed_seconds": round(time.time() - started_at, 3),
        }
        write_private_json(receipt_path, failure_receipt)
        raise
    finally:
        passphrase = ""
        if service_access is not None:
            active_exception = sys.exc_info()[0] is not None
            try:
                service_access.close()
                service_access = None
            except BaseException:
                if not active_exception:
                    raise
        if stack_started and compose and not (completed and args.keep_running):
            active_exception = sys.exc_info()[0] is not None
            try:
                _run(
                    [*compose, "down", "--remove-orphans", "--volumes"],
                    cwd=args.runtime_root,
                    log_path=raw_dir / "compose-down.log",
                    timeout=600,
                    env=compose_env,
                )
                _assert_compose_resources_removed(project, env=compose_env)
                stack_started = False
            except BaseException:
                if not active_exception:
                    raise
        if (
            not stack_started
            and args.runtime_root.exists()
            and not (completed and args.keep_running)
        ):
            active_exception = sys.exc_info()[0] is not None
            try:
                _remove_runtime_root(args.runtime_root)
            except BaseException:
                if not active_exception:
                    raise


if __name__ == "__main__":
    raise SystemExit(main())
