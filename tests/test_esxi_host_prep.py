from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from admin_service.services.esxi_host_prep import ESXiHostPrepService
from app.models.domain import ESXiHostPrepInstallRequest
from app.services.ssh_probe import SSHCommandResult


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
        self.uploads.append((local_path, remote_path))
        if self.put_error is not None:
            raise self.put_error

    def __enter__(self) -> "FakeSFTP":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeClient:
    def __init__(
        self,
        command_results: dict[str, tuple[int, str, str]],
        *,
        open_sftp_error: Exception | None = None,
        put_error: Exception | None = None,
        command_errors: dict[str, Exception] | None = None,
        block_command: str | None = None,
        block_entered: threading.Event | None = None,
        block_release: threading.Event | None = None,
    ) -> None:
        self.command_results = command_results
        self.commands: list[str] = []
        self.sftp = FakeSFTP(put_error=put_error)
        self.open_sftp_error = open_sftp_error
        self.command_errors = dict(command_errors or {})
        self.block_command = block_command
        self.block_entered = block_entered
        self.block_release = block_release

    def open_sftp(self) -> FakeSFTP:
        if self.open_sftp_error is not None:
            raise self.open_sftp_error
        return self.sftp

    def exec_command(self, command: str, timeout: int):
        self.commands.append(command)
        if command in self.command_errors:
            raise self.command_errors[command]
        if command == self.block_command:
            assert self.block_entered is not None
            assert self.block_release is not None
            self.block_entered.set()
            if not self.block_release.wait(timeout=5):
                raise AssertionError("Timed out waiting to release blocked test command")
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
    @staticmethod
    def _set_created_at(staged: dict[str, object], created_at: datetime) -> Path:
        package_dir = Path(str(staged["staged_path"])).parent
        meta_path = package_dir / "meta.json"
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata["created_at"] = created_at.isoformat()
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")
        return package_dir

    def test_prune_removes_stale_owned_package_at_ttl_boundary(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(seconds=3600))

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 1, "skipped": 0, "failed": 0, "limited": False},
            )
            self.assertFalse(package_dir.exists())

    def test_prune_keeps_fresh_owned_package(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            package_dir = self._set_created_at(
                staged,
                now - timedelta(seconds=3599),
            )

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 1, "failed": 0, "limited": False},
            )
            self.assertTrue(package_dir.is_dir())

    def test_zero_ttl_disables_pruning(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=0,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=365))

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 0, "failed": 0, "limited": False},
            )
            self.assertTrue(package_dir.is_dir())

    def test_prune_does_not_follow_package_directory_symlink(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            tempfile.TemporaryDirectory() as external_dir,
        ):
            external_service = ESXiHostPrepService(
                external_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            external_package = external_service.stage_package("vendor.vib", b"outside")
            external_package_dir = self._set_created_at(
                external_package,
                now - timedelta(days=2),
            )
            staging_root = Path(temp_dir)
            (staging_root / external_package_dir.name).symlink_to(
                external_package_dir,
                target_is_directory=True,
            )
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 1, "failed": 0, "limited": False},
            )
            self.assertTrue(external_package_dir.is_dir())
            self.assertEqual(
                Path(str(external_package["staged_path"])).read_bytes(),
                b"outside",
            )

    def test_prune_does_not_follow_symlinked_staging_root(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            external_root = base / "external"
            external_service = ESXiHostPrepService(
                str(external_root),
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            external_package = external_service.stage_package("vendor.vib", b"outside")
            external_package_dir = self._set_created_at(
                external_package,
                now - timedelta(days=2),
            )
            root_link = base / "configured-root"
            root_link.symlink_to(external_root, target_is_directory=True)
            service = ESXiHostPrepService(
                str(root_link),
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 0, "failed": 1, "limited": False},
            )
            self.assertTrue(external_package_dir.is_dir())
            self.assertEqual(
                Path(str(external_package["staged_path"])).read_bytes(),
                b"outside",
            )

    def test_prune_rejects_symlinked_metadata_in_owned_directory(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=2))
            meta_path = package_dir / "meta.json"
            external_meta = Path(temp_dir).parent / f"{package_dir.name}-meta.json"
            external_meta.write_bytes(meta_path.read_bytes())
            meta_path.unlink()
            meta_path.symlink_to(external_meta)
            self.addCleanup(external_meta.unlink, missing_ok=True)

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 1, "failed": 0, "limited": False},
            )
            self.assertTrue(package_dir.is_dir())
            self.assertTrue(meta_path.is_symlink())
            self.assertTrue(external_meta.is_file())

    def test_prune_rejects_symlinked_package_file(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=2))
            package_path = Path(str(staged["staged_path"]))
            external_package = Path(temp_dir).parent / f"{package_dir.name}-vendor.vib"
            external_package.write_bytes(package_path.read_bytes())
            package_path.unlink()
            package_path.symlink_to(external_package)
            self.addCleanup(external_package.unlink, missing_ok=True)

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 1, "failed": 0, "limited": False},
            )
            self.assertTrue(package_dir.is_dir())
            self.assertTrue(package_path.is_symlink())
            self.assertEqual(external_package.read_bytes(), b"payload")

    def test_prune_preserves_owned_directory_with_unrelated_file(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=2))
            unrelated_path = package_dir / "operator-note.txt"
            unrelated_path.write_text("preserve me", encoding="utf-8")

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 1, "failed": 0, "limited": False},
            )
            self.assertEqual(unrelated_path.read_text(encoding="utf-8"), "preserve me")
            self.assertTrue(Path(str(staged["staged_path"])).is_file())

    def test_prune_preserves_unowned_lookalike_directory(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=2))
            lookalike_dir = Path(temp_dir) / "operator-files"
            package_dir.rename(lookalike_dir)

            summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 1, "failed": 0, "limited": False},
            )
            self.assertEqual((lookalike_dir / "vendor.vib").read_bytes(), b"payload")

    def test_prune_keeps_package_while_install_is_active(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=2))
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-vendor.vib"
            install_command = f"esxcli software vib install -v {remote_path} --no-sig-check"
            install_entered = threading.Event()
            install_release = threading.Event()
            FakeProbe.next_client = FakeClient(
                {},
                block_command=install_command,
                block_entered=install_entered,
                block_release=install_release,
            )
            install_errors: list[BaseException] = []

            def run_install() -> None:
                try:
                    service.install_package(
                        ESXiHostPrepInstallRequest(
                            host="192.0.2.10",
                            user="root",
                            password="synthetic",
                            upload_token=str(staged["token"]),
                        )
                    )
                except BaseException as exc:
                    install_errors.append(exc)

            install_thread = threading.Thread(target=run_install)
            install_thread.start()
            self.assertTrue(install_entered.wait(timeout=5))
            try:
                summary = service.prune_stale_packages(now=now)
                self.assertEqual(
                    summary,
                    {"removed": 0, "skipped": 1, "failed": 0, "limited": False},
                )
                self.assertTrue(package_dir.is_dir())
            finally:
                install_release.set()
                install_thread.join(timeout=5)

            self.assertFalse(install_thread.is_alive())
            self.assertEqual(install_errors, [])

    def test_prune_limits_candidates_per_run(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            package_dirs = []
            for filename in ("first.vib", "second.vib"):
                staged = service.stage_package(filename, b"payload")
                package_dirs.append(
                    self._set_created_at(staged, now - timedelta(days=2))
                )

            with patch(
                "admin_service.services.esxi_host_prep.MAX_PRUNE_ENTRIES",
                1,
                create=True,
            ):
                summary = service.prune_stale_packages(now=now)

            self.assertEqual(summary["removed"], 1)
            self.assertTrue(summary["limited"])
            self.assertEqual(sum(package_dir.is_dir() for package_dir in package_dirs), 1)

    def test_prune_reports_cleanup_failure_without_stopping_the_run(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=2))

            with patch.object(
                service,
                "_delete_owned_package",
                side_effect=OSError("synthetic cleanup failure"),
                create=True,
            ):
                summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 0, "skipped": 0, "failed": 1, "limited": False},
            )
            self.assertTrue(package_dir.is_dir())

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
            package_dir = Path(str(staged["staged_path"])).parent
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
            self.assertFalse(package_dir.exists())

    def test_install_package_raises_readable_error_when_remote_upload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            package_dir = Path(str(staged["staged_path"])).parent
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
            self.assertFalse(package_dir.exists())

    def test_open_sftp_failure_does_not_repeat_pre_upload_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            package_dir = Path(str(staged["staged_path"])).parent
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-BCM-vmware-storcli64.zip"
            payload = ESXiHostPrepInstallRequest(
                host="192.0.2.47",
                user="root",
                password="synthetic",
                timeout_seconds=15,
                upload_token=staged["token"],
            )
            FakeProbe.next_client = FakeClient(
                {},
                open_sftp_error=OSError("unable to start sftp subsystem"),
            )
            pre_upload_cleanup = SSHCommandResult(
                command="bounded pre-upload cleanup",
                ok=True,
                stdout="",
                stderr="",
                exit_code=0,
            )

            with patch.object(
                service,
                "_run_remote_command",
                return_value=pre_upload_cleanup,
            ) as run_remote_command:
                with self.assertRaisesRegex(
                    ValueError,
                    r"Remote upload error: unable to start sftp subsystem$",
                ):
                    service.install_package(payload)

            self.assertEqual(run_remote_command.call_count, 1)
            self.assertEqual(
                run_remote_command.call_args.args,
                (FakeProbe.next_client, f"rm -f {remote_path}", payload.timeout_seconds),
            )
            self.assertEqual(FakeProbe.next_client.sftp.uploads, [])
            self.assertFalse(package_dir.exists())

    def test_partial_sftp_upload_failure_is_cleaned_without_masking_upload_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            package_dir = Path(str(staged["staged_path"])).parent
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-BCM-vmware-storcli64.zip"
            payload = ESXiHostPrepInstallRequest(
                host="192.0.2.46",
                user="root",
                password="synthetic",
                timeout_seconds=15,
                upload_token=staged["token"],
            )
            FakeProbe.next_client = FakeClient(
                {},
                put_error=OSError("partial upload failed"),
            )
            pre_upload_cleanup = SSHCommandResult(
                command="bounded pre-upload cleanup",
                ok=True,
                stdout="",
                stderr="",
                exit_code=0,
            )

            with patch.object(
                service,
                "_run_remote_command",
                side_effect=[
                    pre_upload_cleanup,
                    RuntimeError("post-upload cleanup failed"),
                ],
            ) as run_remote_command:
                with self.assertRaisesRegex(
                    ValueError,
                    r"Remote upload error: partial upload failed$",
                ):
                    service.install_package(payload)

            self.assertEqual(run_remote_command.call_count, 2)
            self.assertEqual(
                run_remote_command.call_args_list[1].args,
                (FakeProbe.next_client, f"rm -f {remote_path}", payload.timeout_seconds),
            )
            self.assertEqual(
                FakeProbe.next_client.sftp.uploads,
                [(str(staged["staged_path"]), remote_path)],
            )
            self.assertFalse(package_dir.exists())

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

    def test_install_timeout_preserves_error_when_remote_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("BCM-vmware-storcli64.zip", b"payload")
            package_dir = Path(str(staged["staged_path"])).parent
            remote_path = f"/tmp/truenas-jbod-ui-{staged['token'][:12]}-BCM-vmware-storcli64.zip"
            payload = ESXiHostPrepInstallRequest(
                host="192.0.2.44",
                user="root",
                password="sensitive-password",
                timeout_seconds=15,
                upload_token=staged["token"],
            )
            FakeProbe.next_client = FakeClient({})
            pre_upload_cleanup = SSHCommandResult(
                command="bounded pre-upload cleanup",
                ok=True,
                stdout="",
                stderr="",
                exit_code=0,
            )

            with (
                patch.object(
                    service,
                    "_run_remote_command",
                    side_effect=[
                        pre_upload_cleanup,
                        TimeoutError("original install timeout"),
                        RuntimeError(
                            f"cleanup failed host={payload.host} path={remote_path} "
                            "password=sensitive-password"
                        ),
                    ],
                ) as run_remote_command,
                self.assertLogs(
                    "admin_service.services.esxi_host_prep",
                    level="WARNING",
                ) as captured_logs,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    r"Timed out while installing or verifying .* after 15 seconds",
                ):
                    service.install_package(payload)

            self.assertEqual(run_remote_command.call_count, 3)
            self.assertFalse(package_dir.exists())
            log_text = "\n".join(captured_logs.output)
            self.assertIn("remote cleanup", log_text)
            self.assertNotIn(payload.host, log_text)
            self.assertNotIn(remote_path, log_text)
            self.assertNotIn("sensitive-password", log_text)

    def test_prune_uses_construction_euid_in_host_owned_staging_root(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(
                temp_dir,
                stale_ttl_seconds=3600,
                probe_factory=FakeProbe,
            )
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = self._set_created_at(staged, now - timedelta(days=2))
            root_stat = service.staging_root.stat()
            root_identity = (root_stat.st_dev, root_stat.st_ino)
            service_uid = os.geteuid()
            host_owner_uid = service_uid + 1
            real_fstat = os.fstat

            def fstat_with_host_owned_root(fd: int) -> os.stat_result:
                observed = real_fstat(fd)
                if (observed.st_dev, observed.st_ino) != root_identity:
                    return observed
                fields = list(observed)
                fields[4] = host_owner_uid
                return os.stat_result(fields)

            with (
                patch(
                    "admin_service.services.esxi_host_prep.os.fstat",
                    side_effect=fstat_with_host_owned_root,
                ),
                patch(
                    "admin_service.services.esxi_host_prep.os.geteuid",
                    return_value=host_owner_uid,
                ),
            ):
                summary = service.prune_stale_packages(now=now)

            self.assertEqual(
                summary,
                {"removed": 1, "skipped": 0, "failed": 0, "limited": False},
            )
            self.assertFalse(package_dir.exists())

    def test_duplicate_install_is_rejected_without_midflight_package_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ESXiHostPrepService(temp_dir, probe_factory=FakeProbe)
            staged = service.stage_package("vendor.vib", b"payload")
            package_dir = Path(str(staged["staged_path"])).parent
            payload = ESXiHostPrepInstallRequest(
                host="192.0.2.45",
                user="root",
                password="synthetic",
                upload_token=staged["token"],
            )
            first_install_entered = threading.Event()
            first_install_release = threading.Event()
            install_call_lock = threading.Lock()
            install_call_count = 0
            first_install_errors: list[BaseException] = []

            def block_first_install(*_: object, **__: object) -> dict[str, object]:
                nonlocal install_call_count
                with install_call_lock:
                    install_call_count += 1
                    call_number = install_call_count
                if call_number == 1:
                    first_install_entered.set()
                    if not first_install_release.wait(timeout=5):
                        raise AssertionError("Timed out waiting to release first install")
                return {"ok": True}

            def run_first_install() -> None:
                try:
                    service.install_package(payload)
                except BaseException as exc:
                    first_install_errors.append(exc)

            with patch.object(service, "_install_package", side_effect=block_first_install):
                first_install_thread = threading.Thread(target=run_first_install)
                first_install_thread.start()
                self.assertTrue(first_install_entered.wait(timeout=5))
                try:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"^The selected staged ESXi package is already being installed\.$",
                    ):
                        service.install_package(payload)
                    self.assertTrue(package_dir.is_dir())
                    self.assertEqual(install_call_count, 1)
                finally:
                    first_install_release.set()
                    first_install_thread.join(timeout=5)

            self.assertFalse(first_install_thread.is_alive())
            self.assertEqual(first_install_errors, [])
            self.assertFalse(package_dir.exists())


if __name__ == "__main__":
    unittest.main()
