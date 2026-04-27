from __future__ import annotations

import json
import re
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import SSHConfig
from app.models.domain import ESXiHostPrepInstallRequest
from app.services.ssh_probe import SSHCommandResult, SSHProbe


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS: dict[str, str] = {
    ".zip": "component_bundle",
    ".vib": "vib",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ESXiHostPrepService:
    def __init__(
        self,
        staging_root: str,
        *,
        probe_factory: Callable[[SSHConfig], SSHProbe] = SSHProbe,
    ) -> None:
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.probe_factory = probe_factory

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

    def install_package(self, payload: ESXiHostPrepInstallRequest) -> dict[str, Any]:
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
            known_hosts_path=payload.known_hosts_path,
            strict_host_key_checking=payload.strict_host_key_checking,
            timeout_seconds=payload.timeout_seconds,
            commands=[],
        )
        probe = self.probe_factory(ssh_config)
        remote_name = self._build_remote_filename(str(package["token"]), str(package["filename"]))
        remote_path = f"/tmp/{remote_name}"

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
                try:
                    with client.open_sftp() as sftp:
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
                cleanup_result = self._run_remote_command(
                    client,
                    f"rm -f {shlex.quote(remote_path)}",
                    payload.timeout_seconds,
                )
        except TimeoutError as exc:
            raise ValueError(
                f"Timed out while installing or verifying {filename} on ESXi host {payload.host} "
                f"after {payload.timeout_seconds} seconds. ESXi package apply plus post-install "
                "verification can take longer on some hosts; retry with a larger host-prep timeout."
            ) from exc

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
        if not package_dir.exists() or not package_dir.is_dir():
            return None
        meta_path = package_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        filename = str(metadata.get("filename") or "").strip()
        if not filename:
            return None
        staged_path = package_dir / filename
        if not staged_path.exists():
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
