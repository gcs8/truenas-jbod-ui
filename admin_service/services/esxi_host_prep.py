from __future__ import annotations

import json
import logging
import os
import re
import shlex
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from app.config import SSHConfig
from app.models.domain import ESXiHostPrepInstallRequest
from app.services.ssh_probe import SSHCommandResult, SSHProbe


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_PRUNE_ENTRIES = 1000
MAX_METADATA_BYTES = 16 * 1024
ALLOWED_UPLOAD_EXTENSIONS: dict[str, str] = {
    ".zip": "component_bundle",
    ".vib": "vib",
}
PACKAGE_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _OwnedStagedPackage:
    token: str
    filename: str
    created_at: datetime
    directory_identity: tuple[int, int]
    metadata_identity: tuple[int, int]
    package_identity: tuple[int, int]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ESXiHostPrepService:
    def __init__(
        self,
        staging_root: str,
        *,
        stale_ttl_seconds: int = 24 * 60 * 60,
        probe_factory: Callable[[SSHConfig], SSHProbe] = SSHProbe,
    ) -> None:
        self._service_uid = os.geteuid()
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.stale_ttl_seconds = stale_ttl_seconds
        self.probe_factory = probe_factory
        self._activity_lock = threading.RLock()
        self._active_tokens: dict[str, int] = {}

    def prune_stale_packages(self, *, now: datetime | None = None) -> dict[str, int | bool]:
        summary: dict[str, int | bool] = {
            "removed": 0,
            "skipped": 0,
            "failed": 0,
            "limited": False,
        }
        if self.stale_ttl_seconds == 0:
            return summary
        current_time = now or utcnow()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("Host-prep cleanup requires an aware current time.")
        cutoff = current_time - timedelta(seconds=self.stale_ttl_seconds)
        root_fd: int | None = None
        try:
            root_lstat = self.staging_root.lstat()
            if not stat.S_ISDIR(root_lstat.st_mode):
                summary["failed"] = 1
                return summary
            root_fd = os.open(
                self.staging_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_fstat = os.fstat(root_fd)
            root_identity = (root_lstat.st_dev, root_lstat.st_ino)
            if (root_fstat.st_dev, root_fstat.st_ino) != root_identity:
                summary["failed"] = 1
                return summary

            with os.scandir(root_fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_PRUNE_ENTRIES:
                        summary["limited"] = True
                        break
                    token = entry.name
                    with self._activity_lock:
                        if self._active_tokens.get(token, 0) > 0:
                            summary["skipped"] = int(summary["skipped"]) + 1
                            continue
                        try:
                            package = self._load_owned_package_for_cleanup(
                                root_fd,
                                root_fstat,
                                token,
                            )
                        except OSError:
                            summary["failed"] = int(summary["failed"]) + 1
                            continue
                        if package is None or package.created_at > cutoff:
                            summary["skipped"] = int(summary["skipped"]) + 1
                            continue
                        try:
                            self._delete_owned_package(root_fd, root_fstat, package)
                        except OSError:
                            summary["failed"] = int(summary["failed"]) + 1
                        else:
                            summary["removed"] = int(summary["removed"]) + 1
        except OSError:
            summary["failed"] = int(summary["failed"]) + 1
        finally:
            if root_fd is not None:
                os.close(root_fd)
        return summary

    def _load_owned_package_for_cleanup(
        self,
        root_fd: int,
        root_stat: os.stat_result,
        token: str,
    ) -> _OwnedStagedPackage | None:
        if PACKAGE_TOKEN_PATTERN.fullmatch(token) is None:
            return None
        directory_lstat = os.stat(token, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_lstat.st_mode)
            or directory_lstat.st_dev != root_stat.st_dev
            or directory_lstat.st_uid != self._service_uid
        ):
            return None

        package_fd = os.open(
            token,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            directory_fstat = os.fstat(package_fd)
            directory_identity = (directory_lstat.st_dev, directory_lstat.st_ino)
            if (directory_fstat.st_dev, directory_fstat.st_ino) != directory_identity:
                return None

            entry_names: list[str] = []
            with os.scandir(package_fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2:
                        return None
                    entry_names.append(entry.name)
            if len(entry_names) != 2 or "meta.json" not in entry_names:
                return None

            metadata_lstat = os.stat(
                "meta.json",
                dir_fd=package_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(metadata_lstat.st_mode)
                or metadata_lstat.st_dev != root_stat.st_dev
                or metadata_lstat.st_uid != self._service_uid
                or metadata_lstat.st_nlink != 1
                or metadata_lstat.st_size > MAX_METADATA_BYTES
            ):
                return None
            metadata_fd = os.open(
                "meta.json",
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=package_fd,
            )
            try:
                metadata_fstat = os.fstat(metadata_fd)
                metadata_identity = (metadata_lstat.st_dev, metadata_lstat.st_ino)
                if (metadata_fstat.st_dev, metadata_fstat.st_ino) != metadata_identity:
                    return None
                with os.fdopen(metadata_fd, "rb", closefd=True) as metadata_file:
                    metadata_fd = -1
                    raw_metadata = metadata_file.read(MAX_METADATA_BYTES + 1)
            finally:
                if metadata_fd >= 0:
                    os.close(metadata_fd)
            if len(raw_metadata) > MAX_METADATA_BYTES:
                return None
            try:
                metadata = json.loads(raw_metadata.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            expected_keys = {
                "token",
                "filename",
                "extension",
                "install_mode",
                "size_bytes",
                "created_at",
            }
            if not isinstance(metadata, dict) or set(metadata) != expected_keys:
                return None
            filename = metadata["filename"]
            if not isinstance(filename, str):
                return None
            try:
                if self._sanitize_filename(filename) != filename:
                    return None
            except ValueError:
                return None
            extension = Path(filename).suffix.lower()
            if (
                metadata["token"] != token
                or metadata["extension"] != extension
                or metadata["install_mode"] != ALLOWED_UPLOAD_EXTENSIONS.get(extension)
                or type(metadata["size_bytes"]) is not int
                or type(metadata["created_at"]) is not str
                or set(entry_names) != {"meta.json", filename}
            ):
                return None

            package_lstat = os.stat(
                filename,
                dir_fd=package_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(package_lstat.st_mode)
                or package_lstat.st_dev != root_stat.st_dev
                or package_lstat.st_uid != self._service_uid
                or package_lstat.st_nlink != 1
                or package_lstat.st_size != metadata["size_bytes"]
            ):
                return None
            try:
                created_at = datetime.fromisoformat(metadata["created_at"])
            except ValueError:
                return None
            if created_at.tzinfo is None or created_at.utcoffset() is None:
                return None
            return _OwnedStagedPackage(
                token=token,
                filename=filename,
                created_at=created_at,
                directory_identity=directory_identity,
                metadata_identity=metadata_identity,
                package_identity=(package_lstat.st_dev, package_lstat.st_ino),
            )
        finally:
            os.close(package_fd)

    def _remove_completed_package(self, token: str) -> str:
        root_fd: int | None = None
        try:
            root_lstat = self.staging_root.lstat()
            if not stat.S_ISDIR(root_lstat.st_mode):
                return "skipped"
            root_fd = os.open(
                self.staging_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_fstat = os.fstat(root_fd)
            if (root_fstat.st_dev, root_fstat.st_ino) != (
                root_lstat.st_dev,
                root_lstat.st_ino,
            ):
                return "skipped"
            package = self._load_owned_package_for_cleanup(
                root_fd,
                root_fstat,
                token,
            )
            if package is None:
                return "skipped"
            self._delete_owned_package(root_fd, root_fstat, package)
        except OSError:
            return "failed"
        finally:
            if root_fd is not None:
                os.close(root_fd)
        return "removed"

    @staticmethod
    def _delete_owned_package(
        root_fd: int,
        root_stat: os.stat_result,
        package: _OwnedStagedPackage,
    ) -> None:
        directory_lstat = os.stat(
            package.token,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(directory_lstat.st_mode)
            or (directory_lstat.st_dev, directory_lstat.st_ino)
            != package.directory_identity
            or directory_lstat.st_dev != root_stat.st_dev
        ):
            raise OSError("Host-prep package directory changed before cleanup.")
        package_fd = os.open(
            package.token,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            if (os.fstat(package_fd).st_dev, os.fstat(package_fd).st_ino) != package.directory_identity:
                raise OSError("Host-prep package directory changed before cleanup.")
            with os.scandir(package_fd) as entries:
                entry_names = {entry.name for entry in entries}
            if entry_names != {"meta.json", package.filename}:
                raise OSError("Host-prep package contents changed before cleanup.")
            metadata_lstat = os.stat(
                "meta.json",
                dir_fd=package_fd,
                follow_symlinks=False,
            )
            package_lstat = os.stat(
                package.filename,
                dir_fd=package_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(metadata_lstat.st_mode)
                or (metadata_lstat.st_dev, metadata_lstat.st_ino)
                != package.metadata_identity
                or not stat.S_ISREG(package_lstat.st_mode)
                or (package_lstat.st_dev, package_lstat.st_ino)
                != package.package_identity
            ):
                raise OSError("Host-prep package contents changed before cleanup.")
            os.unlink(package.filename, dir_fd=package_fd)
            os.unlink("meta.json", dir_fd=package_fd)
        finally:
            os.close(package_fd)
        os.rmdir(package.token, dir_fd=root_fd)

    @contextmanager
    def _active_package(self, token: str) -> Iterator[None]:
        cleaned_token = str(token or "").strip()
        with self._activity_lock:
            if self._active_tokens.get(cleaned_token, 0) > 0:
                raise ValueError(
                    "The selected staged ESXi package is already being installed."
                )
            self._active_tokens[cleaned_token] = 1
        try:
            yield
        finally:
            with self._activity_lock:
                self._active_tokens.pop(cleaned_token, None)

    def list_staged_packages(self) -> list[dict[str, Any]]:
        packages: list[dict[str, Any]] = []
        for package_dir in self.staging_root.iterdir():
            package = self._load_package(package_dir)
            if package is not None:
                packages.append(package)
        packages.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return packages

    def stage_package(self, filename: str, content: bytes) -> dict[str, Any]:
        safe_filename = self._sanitize_filename(filename)
        if not content:
            raise ValueError("The uploaded ESXi package was empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"The uploaded ESXi package is too large ({len(content)} bytes). "
                f"Keep it under {MAX_UPLOAD_BYTES} bytes for this first-pass admin upload flow."
            )
        extension = Path(safe_filename).suffix.lower()
        install_mode = ALLOWED_UPLOAD_EXTENSIONS.get(extension)
        if install_mode is None:
            raise ValueError("Only ESXi .zip offline bundles and .vib packages are supported here.")

        token = uuid.uuid4().hex
        package_dir = self.staging_root / token
        package_dir.mkdir(parents=True, exist_ok=False)
        package_path = package_dir / safe_filename
        package_path.write_bytes(content)
        metadata = {
            "token": token,
            "filename": safe_filename,
            "extension": extension,
            "install_mode": install_mode,
            "size_bytes": len(content),
            "created_at": utcnow().isoformat(),
        }
        (package_dir / "meta.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return self._load_package(package_dir) or metadata

    def install_package(
        self,
        payload: ESXiHostPrepInstallRequest,
        *,
        known_hosts_path: str | None = None,
    ) -> dict[str, Any]:
        with self._active_package(payload.upload_token):
            try:
                return self._install_package(payload, known_hosts_path=known_hosts_path)
            finally:
                cleanup_outcome = self._remove_completed_package(payload.upload_token)
                if cleanup_outcome != "removed":
                    logger.warning(
                        "Host-prep completed-package cleanup did not remove the artifact "
                        "outcome=%s",
                        cleanup_outcome,
                    )

    def _install_package(
        self,
        payload: ESXiHostPrepInstallRequest,
        *,
        known_hosts_path: str | None = None,
    ) -> dict[str, Any]:
        package = self.get_staged_package(payload.upload_token)
        local_path = Path(str(package["staged_path"]))
        filename = str(package.get("filename") or local_path.name)
        ssh_config = SSHConfig(
            enabled=True,
            host=payload.host,
            port=payload.port,
            user=payload.user,
            key_path=payload.key_path or "",
            password=payload.password or "",
            known_hosts_path=known_hosts_path,
            strict_host_key_checking=payload.strict_host_key_checking,
            timeout_seconds=payload.timeout_seconds,
            commands=[],
        )
        probe = self.probe_factory(ssh_config)
        remote_name = self._build_remote_filename(str(package["token"]), str(package["filename"]))
        remote_path = f"/tmp/{remote_name}"

        cleanup_result: SSHCommandResult | None = None
        try:
            with probe.open_client() as client:
                pre_upload_cleanup = self._run_remote_command(
                    client,
                    f"rm -f {shlex.quote(remote_path)}",
                    payload.timeout_seconds,
                )
                if not pre_upload_cleanup.ok:
                    cleanup_detail = (
                        pre_upload_cleanup.stderr.strip()
                        or pre_upload_cleanup.stdout.strip()
                        or "Unknown remote cleanup error."
                    )
                    raise ValueError(
                        f"Unable to clear the previous ESXi temp file at {remote_path} before upload: "
                        f"{cleanup_detail}"
                    )
                remote_cleanup_required = False
                try:
                    try:
                        with client.open_sftp() as sftp:
                            remote_cleanup_required = True
                            sftp.put(str(local_path), remote_path)
                    except Exception as exc:
                        raise ValueError(
                            f"Unable to upload {filename} to {remote_path} on the ESXi host. "
                            "The admin flow clears any previous temp file at that path before upload, "
                            f"so this was not a simple existing-file conflict. Remote upload error: {exc}"
                        ) from exc
                    install_command = self._build_install_command(remote_path, str(package["extension"]))
                    install_result = self._run_remote_command(client, install_command, payload.timeout_seconds)
                    verification = self._run_verification_commands(client, payload.timeout_seconds)
                finally:
                    if remote_cleanup_required:
                        try:
                            cleanup_result = self._run_remote_command(
                                client,
                                f"rm -f {shlex.quote(remote_path)}",
                                payload.timeout_seconds,
                            )
                        except Exception:
                            logger.warning(
                                "Host-prep remote cleanup did not complete outcome=exception"
                            )
                        else:
                            if not cleanup_result.ok:
                                logger.warning(
                                    "Host-prep remote cleanup did not complete "
                                    "outcome=command_failed"
                                )
        except TimeoutError as exc:
            raise ValueError(
                f"Timed out while installing or verifying {filename} on ESXi host {payload.host} "
                f"after {payload.timeout_seconds} seconds. ESXi package apply plus post-install "
                "verification can take longer on some hosts; retry with a larger host-prep timeout."
            ) from exc

        if cleanup_result is None:
            cleanup_result = SSHCommandResult(
                command="remote package cleanup",
                ok=False,
                stdout="",
                stderr="",
                exit_code=-1,
            )
        detail = self._build_install_detail(package, install_result, verification)
        return {
            "ok": install_result.ok,
            "detail": detail,
            "package": package,
            "remote_path": remote_path,
            "install_command": install_command,
            "install_result": self._serialize_command_result(install_result),
            "verification": verification,
            "cleanup_result": self._serialize_command_result(cleanup_result),
        }

    def get_staged_package(self, token: str) -> dict[str, Any]:
        cleaned_token = str(token or "").strip()
        if not cleaned_token:
            raise ValueError("A staged ESXi package token is required.")
        package = self._load_package(self.staging_root / cleaned_token)
        if package is None:
            raise ValueError("The selected staged ESXi package could not be found in the admin temp area.")
        return package

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        base_name = Path(str(filename or "")).name.strip()
        if not base_name:
            raise ValueError("An ESXi package filename is required.")
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", base_name).strip("._-")
        if not sanitized:
            raise ValueError("The ESXi package filename did not contain any safe characters to keep.")
        return sanitized[:255]

    @staticmethod
    def _build_remote_filename(token: str, filename: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("._-") or "package.bin"
        return f"truenas-jbod-ui-{token[:12]}-{safe_name}"[:255]

    @staticmethod
    def _build_install_command(remote_path: str, extension: str) -> str:
        quoted_path = shlex.quote(remote_path)
        if extension == ".zip":
            return f"esxcli software component apply -d {quoted_path}"
        if extension == ".vib":
            return f"esxcli software vib install -v {quoted_path} --no-sig-check"
        raise ValueError(f"Unsupported ESXi package type: {extension}")

    def _load_package(self, package_dir: Path) -> dict[str, Any] | None:
        try:
            if not stat.S_ISDIR(package_dir.lstat().st_mode):
                return None
        except OSError:
            return None
        meta_path = package_dir / "meta.json"
        try:
            if not stat.S_ISREG(meta_path.lstat().st_mode):
                return None
        except OSError:
            return None
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        filename = str(metadata.get("filename") or "").strip()
        if not filename:
            return None
        staged_path = package_dir / filename
        try:
            staged_stat = staged_path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(staged_stat.st_mode):
            return None
        return {
            "token": str(metadata.get("token") or package_dir.name),
            "filename": filename,
            "extension": str(metadata.get("extension") or staged_path.suffix.lower()),
            "install_mode": str(metadata.get("install_mode") or ALLOWED_UPLOAD_EXTENSIONS.get(staged_path.suffix.lower(), "unknown")),
            "size_bytes": int(metadata.get("size_bytes") or staged_path.stat().st_size),
            "created_at": str(metadata.get("created_at") or utcnow().isoformat()),
            "staged_path": str(staged_path),
        }

    @staticmethod
    def _run_remote_command(client: Any, command: str, timeout_seconds: int) -> SSHCommandResult:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout_seconds)
        stdin.close()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return SSHCommandResult(
            command=command,
            ok=exit_code == 0,
            stdout=output,
            stderr=error,
            exit_code=exit_code,
        )

    def _run_verification_commands(self, client: Any, timeout_seconds: int) -> dict[str, Any]:
        command_map = {
            "component_list": "esxcli software component list | grep -i storcli || true",
            "vib_list": "esxcli software vib list | grep -i storcli || true",
            "storcli_paths": "find /opt/lsi -name 'storcli*' 2>/dev/null || true",
            "storcli_show": "/opt/lsi/storcli64/storcli64 show J 2>&1 || true",
            "adapter_list": "esxcli storage core adapter list 2>&1 || true",
            "pcipassthru_list": "esxcli hardware pci pcipassthru list 2>&1 || true",
            "megaraid_pci": "lspci 2>&1 | grep -i 'MegaRAID' || true",
        }
        results = {
            name: self._serialize_command_result(self._run_remote_command(client, command, timeout_seconds))
            for name, command in command_map.items()
        }
        storcli_text = "\n".join(
            [
                str(results["component_list"].get("stdout") or ""),
                str(results["vib_list"].get("stdout") or ""),
                str(results["storcli_paths"].get("stdout") or ""),
                str(results["storcli_show"].get("stdout") or ""),
                str(results["storcli_show"].get("stderr") or ""),
            ]
        )
        controller_count = self._extract_controller_count(storcli_text)
        megaraid_pci_addresses = self._extract_megaraid_pci_addresses(str(results["megaraid_pci"].get("stdout") or ""))
        passthrough_enabled_addresses = self._extract_enabled_passthrough_addresses(
            str(results["pcipassthru_list"].get("stdout") or "")
        )
        megaraid_passthrough_addresses = [
            address
            for address in megaraid_pci_addresses
            if address in passthrough_enabled_addresses
        ]
        results["summary"] = {
            "storcli_installed": "storcli" in storcli_text.lower(),
            "controller_count": controller_count,
            "controller_visible": bool(controller_count and controller_count > 0),
            "megaraid_pci_addresses": megaraid_pci_addresses,
            "passthrough_enabled_addresses": passthrough_enabled_addresses,
            "megaraid_passthrough_addresses": megaraid_passthrough_addresses,
            "detail": self._build_verification_detail(
                storcli_text,
                controller_count,
                megaraid_passthrough_addresses,
            ),
        }
        return results

    @staticmethod
    def _serialize_command_result(result: SSHCommandResult) -> dict[str, Any]:
        return {
            "command": result.command,
            "ok": result.ok,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "exit_code": result.exit_code,
        }

    @staticmethod
    def _extract_controller_count(output: str) -> int | None:
        match = re.search(r'"Number of Controllers"\s*:\s*(\d+)', output)
        if match:
            return int(match.group(1))
        match = re.search(r"Number of Controllers\s*=\s*(\d+)", output)
        if match:
            return int(match.group(1))
        return None

    @classmethod
    def _extract_megaraid_pci_addresses(cls, output: str) -> list[str]:
        addresses: list[str] = []
        for raw_line in output.splitlines():
            match = re.search(r"(?im)^\s*([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\b", raw_line)
            if not match:
                continue
            address = match.group(1).lower()
            if address not in addresses:
                addresses.append(address)
        return addresses

    @classmethod
    def _extract_enabled_passthrough_addresses(cls, output: str) -> list[str]:
        addresses: list[str] = []
        for raw_line in output.splitlines():
            match = re.search(
                r"(?im)^\s*([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\s+true\b",
                raw_line,
            )
            if not match:
                continue
            address = match.group(1).lower()
            if address not in addresses:
                addresses.append(address)
        return addresses

    @classmethod
    def _build_verification_detail(
        cls,
        storcli_text: str,
        controller_count: int | None,
        megaraid_passthrough_addresses: list[str] | None = None,
    ) -> str:
        normalized = storcli_text.lower()
        if controller_count and controller_count > 0:
            return f"StorCLI can see {controller_count} controller(s) on this ESXi host."
        if megaraid_passthrough_addresses:
            address_list = ", ".join(megaraid_passthrough_addresses)
            return (
                "StorCLI is present, but the Broadcom MegaRAID controller is currently configured for "
                f"PCI passthrough on this ESXi host ({address_list}). ESXi will not bind that device to "
                "lsi_mr3 or expose it to StorCLI until passthrough is disabled and the host is rebooted."
            )
        if "controller 0 not found" in normalized or "no controller found" in normalized or controller_count == 0:
            return (
                "StorCLI is present, but no compatible MegaRAID controller is currently visible to it on this ESXi host."
            )
        if "storcli" in normalized:
            return "StorCLI package or binary paths are visible on this ESXi host."
        return "StorCLI verification did not find a visible package, binary, or controller yet."

    @classmethod
    def _build_install_detail(
        cls,
        package: dict[str, Any],
        install_result: SSHCommandResult,
        verification: dict[str, Any],
    ) -> str:
        filename = str(package.get("filename") or "package")
        if not install_result.ok:
            error_detail = install_result.stderr.strip() or install_result.stdout.strip() or "Unknown ESXi install error."
            return f"Uploaded {filename}, but the ESXi install command failed: {error_detail}"
        verification_summary = verification.get("summary") if isinstance(verification, dict) else {}
        detail = (
            verification_summary.get("detail")
            if isinstance(verification_summary, dict)
            else None
        )
        if detail:
            return f"Uploaded {filename} and completed the ESXi install command. {detail}"
        return f"Uploaded {filename} and completed the ESXi install command."
