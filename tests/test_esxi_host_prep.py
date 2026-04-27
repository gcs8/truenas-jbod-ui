from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from admin_service.services.esxi_host_prep import ESXiHostPrepService
from app.models.domain import ESXiHostPrepInstallRequest


class FakeChannel:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def recv_exit_status(self) -> int:
        return self._exit_code


class FakeStream:
    def __init__(self, content: str, exit_code: int = 0) -> None:
        self._content = content.encode("utf-8")
        self.channel = FakeChannel(exit_code)

    def read(self) -> bytes:
        return self._content


class FakeStdin:
    def close(self) -> None:
        return None


class FakeSFTP:
    def __init__(self, *, put_error: Exception | None = None) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.put_error = put_error

    def put(self, local_path: str, remote_path: str) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.uploads.append((local_path, remote_path))

    def __enter__(self) -> "FakeSFTP":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeClient:
    def __init__(
        self,
        command_results: dict[str, tuple[int, str, str]],
        *,
        put_error: Exception | None = None,
        command_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.command_results = command_results
        self.commands: list[str] = []
        self.sftp = FakeSFTP(put_error=put_error)
        self.command_errors = dict(command_errors or {})

    def open_sftp(self) -> FakeSFTP:
        return self.sftp

    def exec_command(self, command: str, timeout: int):
        self.commands.append(command)
        if command in self.command_errors:
            raise self.command_errors[command]
        exit_code, stdout, stderr = self.command_results.get(command, (0, "", ""))
        return FakeStdin(), FakeStream(stdout, exit_code), FakeStream(stderr, exit_code)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeProbe:
    next_client: FakeClient | None = None
    last_config = None

    def __init__(self, config) -> None:
        type(self).last_config = config

    def open_client(self) -> FakeClient:
        if type(self).next_client is None:
            raise AssertionError("FakeProbe.next_client must be set before open_client()")
        return type(self).next_client


class ESXiHostPrepServiceTests(unittest.TestCase):
    def test_stage_package_rejects_unsupported_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)

            with self.assertRaisesRegex(ValueError, r"\.zip offline bundles and \.vib"):
                service.stage_package("storcli.txt", b"not valid")

    def test_stage_package_records_metadata_and_lists_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)

            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            packages = service.list_staged_packages()

            self.assertEqual(staged["filename"], "BCM-vmware-storcli64.zip")
            self.assertEqual(staged["extension"], ".zip")
            self.assertEqual(staged["install_mode"], "component_bundle")
            self.assertEqual(staged["size_bytes"], 7)
            self.assertTrue(Path(staged["staged_path"]).exists())
            self.assertEqual(len(packages), 1)
            self.assertEqual(packages[0]["token"], staged["token"])

    def test_install_package_uses_component_apply_for_zip_and_reports_zero_visible_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-BCM-vmware-storcli64.zip"
            install_command = f"esxcli software component apply -d {remote_path}"
            FakeProbe.next_client = FakeClient(
                {
                    f"rm -f {remote_path}": (0, "", ""),
                    install_command: (0, "Installation Result\nMessage: Operation finished successfully.\n", ""),
                    "esxcli software component list | grep -i storcli || true": (0, "BCM-vmware-storcli64\n", ""),
                    "esxcli software vib list | grep -i storcli || true": (0, "vmware-storcli64\n", ""),
                    "find /opt/lsi -name 'storcli*' 2>/dev/null || true": (0, "/opt/lsi/storcli64/storcli64\n", ""),
                    "/opt/lsi/storcli64/storcli64 show J 2>&1 || true": (
                        0,
                        "CLI Version = 007.2705.0000.0000\nNumber of Controllers = 0\n",
                        "",
                    ),
                    "esxcli storage core adapter list 2>&1 || true": (0, "vmhba0 vmw_ahci\n", ""),
                    "esxcli hardware pci pcipassthru list 2>&1 || true": (
                        0,
                        "Device ID     Enabled\n------------  -------\n0000:3b:00.0     true\n",
                        "",
                    ),
                    "lspci 2>&1 | grep -i 'MegaRAID' || true": (
                        0,
                        "0000:3b:00.0 RAID bus controller: Broadcom MegaRAID SAS Invader Controller [vmhba2]\n",
                        "",
                    ),
                }
            )

            result = service.install_package(
                ESXiHostPrepInstallRequest(
                    host="10.13.37.121",
                    user="root",
                    password="secret",
                    upload_token=staged["token"],
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["install_command"], install_command)
            self.assertEqual(result["remote_path"], remote_path)
            self.assertIn("PCI passthrough", result["detail"])
            self.assertFalse(result["verification"]["summary"]["controller_visible"])
            self.assertEqual(result["verification"]["summary"]["controller_count"], 0)
            self.assertEqual(
                result["verification"]["summary"]["megaraid_passthrough_addresses"],
                ["0000:3b:00.0"],
            )
            self.assertEqual(FakeProbe.last_config.host, "10.13.37.121")
            self.assertEqual(FakeProbe.last_config.password, "secret")
            self.assertEqual(FakeProbe.next_client.sftp.uploads[0][1], remote_path)

    def test_install_package_uses_vib_install_for_vib_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("vmware-storcli64.vib", b"payload")
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-vmware-storcli64.vib"
            install_command = f"esxcli software vib install -v {remote_path} --no-sig-check"
            FakeProbe.next_client = FakeClient(
                {
                    f"rm -f {remote_path}": (0, "", ""),
                    install_command: (0, "Message: Operation finished successfully.\n", ""),
                    "esxcli software component list | grep -i storcli || true": (0, "", ""),
                    "esxcli software vib list | grep -i storcli || true": (0, "vmware-storcli64\n", ""),
                    "find /opt/lsi -name 'storcli*' 2>/dev/null || true": (0, "/opt/lsi/storcli64/storcli64\n", ""),
                    "/opt/lsi/storcli64/storcli64 show J 2>&1 || true": (0, "Number of Controllers = 1\n", ""),
                    "esxcli storage core adapter list 2>&1 || true": (0, "vmhba2 lsi_mr3\n", ""),
                    "esxcli hardware pci pcipassthru list 2>&1 || true": (0, "", ""),
                    "lspci 2>&1 | grep -i 'MegaRAID' || true": (0, "", ""),
                }
            )

            result = service.install_package(
                ESXiHostPrepInstallRequest(
                    host="10.13.37.121",
                    user="root",
                    password="secret",
                    upload_token=staged["token"],
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["install_command"], install_command)
            self.assertTrue(result["verification"]["summary"]["controller_visible"])
            self.assertEqual(result["verification"]["summary"]["controller_count"], 1)

    def test_install_package_raises_readable_error_when_remote_upload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-BCM-vmware-storcli64.zip"
            FakeProbe.next_client = FakeClient(
                {
                    f"rm -f {remote_path}": (0, "", ""),
                },
                put_error=OSError("Permission denied"),
            )

            with self.assertRaisesRegex(ValueError, r"not a simple existing-file conflict"):
                service.install_package(
                    ESXiHostPrepInstallRequest(
                        host="10.13.37.121",
                        user="root",
                        password="secret",
                        upload_token=staged["token"],
                    )
                )

    def test_install_package_raises_readable_error_when_remote_command_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-BCM-vmware-storcli64.zip"
            install_command = f"esxcli software component apply -d {remote_path}"
            FakeProbe.next_client = FakeClient(
                {
                    f"rm -f {remote_path}": (0, "", ""),
                },
                command_errors={
                    install_command: TimeoutError("timed out"),
                },
            )

            with self.assertRaisesRegex(ValueError, r"Timed out while installing or verifying .* after 15 seconds"):
                service.install_package(
                    ESXiHostPrepInstallRequest(
                        host="10.13.37.122",
                        user="root",
                        password="secret",
                        timeout_seconds=15,
                        upload_token=staged["token"],
                    )
                )


if __name__ == "__main__":
    unittest.main()
