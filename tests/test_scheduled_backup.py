from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from history_service.scheduled_backup import (
    ScheduledBackupRunner,
    ScheduledBackupSettings,
)
from history_service.system_backup import FileBackupArtifact


class ScheduledBackupSettingsTests(unittest.TestCase):
    def test_schedule_is_disabled_by_default_and_independent_of_admin_settings(self) -> None:
        settings = ScheduledBackupSettings()

        self.assertFalse(settings.enabled)
        self.assertIsNone(settings.destination_dir)
        self.assertIsNone(settings.status_file)
        self.assertEqual(settings.retention_count, 0)
        self.assertEqual(settings.included_groups, [])
        self.assertIsNone(settings.passphrase_file)

        admin_config = (Path(__file__).resolve().parents[1] / "admin_service/config.py").read_text(
            encoding="utf-8"
        )
        admin_main = (Path(__file__).resolve().parents[1] / "admin_service/main.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("scheduled_backup", admin_config)
        self.assertNotIn("ScheduledBackup", admin_main)

    def test_enabled_schedule_requires_explicit_one_shot_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "destination"):
            ScheduledBackupSettings(enabled=True)
        with self.assertRaisesRegex(ValueError, "status"):
            ScheduledBackupSettings(enabled=True, destination_dir="/var/backups/jbod")
        with self.assertRaisesRegex(ValueError, "retention"):
            ScheduledBackupSettings(
                enabled=True,
                destination_dir="/var/backups/jbod",
                status_file="/var/lib/jbod-backup/status.json",
            )
        with self.assertRaisesRegex(ValueError, "included groups"):
            ScheduledBackupSettings(
                enabled=True,
                destination_dir="/var/backups/jbod",
                status_file="/var/lib/jbod-backup/status.json",
                retention_count=7,
            )
        with self.assertRaisesRegex(ValueError, "passphrase file"):
            ScheduledBackupSettings(
                enabled=True,
                destination_dir="/var/backups/jbod",
                status_file="/var/lib/jbod-backup/status.json",
                retention_count=7,
                included_groups=["config_file", "mapping_file"],
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            ScheduledBackupSettings(
                enabled=True,
                destination_dir="/var/backups/jbod",
                status_file="/var/lib/jbod-backup/status.json",
                retention_count=7,
                included_groups=["mapping_file", "mapping_file"],
                passphrase_file="/run/backup-secrets/passphrase",
            )

        settings = ScheduledBackupSettings(
            enabled=True,
            destination_dir="/var/backups/jbod",
            status_file="/var/lib/jbod-backup/status.json",
            retention_count=7,
            included_groups=["config_file", "mapping_file"],
            passphrase_file="/run/backup-secrets/passphrase",
        )
        self.assertTrue(settings.enabled)


class ScheduledBackupRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="scheduled-backup-test-"))
        self.destination = self.temp_dir / "destination"
        self.status_dir = self.temp_dir / "status"
        self.status_file = self.status_dir / "scheduled-backup.json"
        self.passphrase_file = self.temp_dir / "passphrase"
        self.passphrase_file.write_text("correct horse battery staple\n", encoding="utf-8")
        self.passphrase_file.chmod(0o600)
        self.workspace = self.temp_dir / "artifact-workspace"
        self.workspace.mkdir()
        self.artifact_path = self.workspace / "source.tar.zst.enc"
        self.artifact_path.write_bytes(b"authenticated-encrypted-archive")
        self.backup_service = MagicMock()
        self.backup_service.export_scheduled_bundle_to_file.return_value = FileBackupArtifact(
            filename="source.tar.zst.enc",
            path=self.artifact_path,
            media_type="application/octet-stream",
            manifest={"schema_version": 1},
            cleanup_root=self.workspace,
        )
        self.backup_service.preflight_scheduled_bundle_file.return_value = {
            "selected_groups": ["config_file", "mapping_file", "profile_file"],
            "absent_groups": [],
        }
        self.now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _runner(self, **overrides: object) -> ScheduledBackupRunner:
        payload: dict[str, object] = {
            "backup_service": self.backup_service,
            "destination_dir": self.destination,
            "status_file": self.status_file,
            "passphrase_file": self.passphrase_file,
            "included_groups": ["config_file", "mapping_file", "profile_file"],
            "retention_count": 2,
            "clock": lambda: self.now,
        }
        payload.update(overrides)
        return ScheduledBackupRunner(**payload)

    def _published_archives(self) -> list[Path]:
        if not self.destination.exists():
            return []
        return sorted(self.destination.glob("jbod-scheduled-backup-*.tar.zst.enc"))

    def test_run_once_preflights_copy_then_publishes_private_archive_and_status(self) -> None:
        runner = self._runner()

        result = runner.run_once()

        archives = self._published_archives()
        self.assertEqual(len(archives), 1)
        self.assertRegex(
            archives[0].name,
            r"^jbod-scheduled-backup-20300102T030405Z-[0-9a-f]{8}\.tar\.zst\.enc$",
        )
        self.assertEqual(archives[0].read_bytes(), b"authenticated-encrypted-archive")
        self.assertEqual(stat.S_IMODE(archives[0].stat().st_mode), 0o600)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.destination.iterdir()))
        self.backup_service.export_scheduled_bundle_to_file.assert_called_once_with(
            passphrase="correct horse battery staple",
            included_paths=["config_file", "mapping_file", "profile_file"],
        )
        preflight_path = self.backup_service.preflight_scheduled_bundle_file.call_args.args[0]
        self.assertEqual(Path(preflight_path).parent, self.destination)
        self.assertEqual(
            self.backup_service.preflight_scheduled_bundle_file.call_args.kwargs,
            {
                "passphrase": "correct horse battery staple",
                "expected_groups": ["config_file", "mapping_file", "profile_file"],
            },
        )
        self.assertFalse(self.workspace.exists())
        self.assertEqual(result["last_success_at"], "2030-01-02T03:04:05+00:00")
        self.assertEqual(result["last_size_bytes"], len(b"authenticated-encrypted-archive"))
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        persisted = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted, result)
        self.assertEqual(stat.S_IMODE(self.status_file.stat().st_mode), 0o600)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("correct horse battery staple", serialized)
        self.assertNotIn(str(self.passphrase_file), serialized)
        self.assertNotIn(str(self.destination), serialized)

    def test_retention_removes_only_old_owned_regular_archives_after_success(self) -> None:
        self.destination.mkdir(mode=0o700)
        owned = (
            "jbod-scheduled-backup-20290101T000000Z-00000001.tar.zst.enc",
            "jbod-scheduled-backup-20290201T000000Z-00000002.tar.zst.enc",
        )
        for name in owned:
            path = self.destination / name
            path.write_bytes(name.encode("ascii"))
            path.chmod(0o600)
        unrelated = self.destination / "unrelated.tar.zst.enc"
        unrelated.write_bytes(b"keep")
        symlink = self.destination / "jbod-scheduled-backup-20280101T000000Z-00000000.tar.zst.enc"
        symlink.symlink_to(unrelated)

        result = self._runner().run_once()

        names = {path.name for path in self.destination.iterdir()}
        self.assertNotIn(owned[0], names)
        self.assertIn(owned[1], names)
        self.assertIn(unrelated.name, names)
        self.assertIn(symlink.name, names)
        self.assertEqual(result["last_retention_removed"], 1)

    def test_passphrase_file_rejects_unsafe_modes_symlinks_nul_and_ambiguous_newlines(self) -> None:
        self.passphrase_file.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            self._runner().run_once()
        status = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["last_error_code"], "ValueError")

        self.passphrase_file.unlink()
        target = self.temp_dir / "actual-passphrase"
        target.write_text("secret", encoding="utf-8")
        target.chmod(0o600)
        self.passphrase_file.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            self._runner().run_once()

        self.passphrase_file.unlink()
        self.passphrase_file.write_bytes(b"secret\x00value")
        self.passphrase_file.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "invalid characters"):
            self._runner().run_once()

        self.passphrase_file.write_text("first\nsecond\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ambiguous newline"):
            self._runner().run_once()

    def test_destination_and_status_directories_must_be_private(self) -> None:
        self.destination.mkdir(mode=0o777)
        self.destination.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "private directory"):
            self._runner().run_once()
        self.backup_service.export_scheduled_bundle_to_file.assert_not_called()

        self.destination.chmod(0o700)
        self.status_dir.mkdir(mode=0o777)
        self.status_dir.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "status directory"):
            self._runner().run_once()

    def test_failure_writes_sanitized_durable_status_and_preserves_last_success(self) -> None:
        runner = self._runner()
        first = runner.run_once()
        first_name = first["last_artifact_name"]
        self.now += timedelta(hours=1)
        self.workspace.mkdir()
        self.artifact_path.write_bytes(b"next")
        self.backup_service.export_scheduled_bundle_to_file.side_effect = RuntimeError(
            "secret value and private path"
        )

        with self.assertRaises(RuntimeError):
            runner.run_once()

        status = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertEqual(status["last_error_code"], "RuntimeError")
        self.assertEqual(status["last_failure_at"], "2030-01-02T04:04:05+00:00")
        self.assertEqual(status["last_success_at"], "2030-01-02T03:04:05+00:00")
        self.assertEqual(status["last_artifact_name"], first_name)
        self.assertEqual(status["success_count"], 1)
        self.assertEqual(status["failure_count"], 1)
        self.assertNotIn("secret value", json.dumps(status))
        self.assertEqual(len(self._published_archives()), 1)

    def test_malformed_durable_status_fails_before_export_and_is_not_overwritten(self) -> None:
        self.status_file.parent.mkdir(mode=0o700)
        malformed = b'{"schema_version":1,"success_count":"not-an-integer"}\n'
        self.status_file.write_bytes(malformed)
        self.status_file.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "status is invalid"):
            self._runner().run_once()

        self.backup_service.export_scheduled_bundle_to_file.assert_not_called()
        self.assertEqual(self.status_file.read_bytes(), malformed)

    def test_concurrent_runners_use_destination_lock(self) -> None:
        export_started = threading.Event()
        release_export = threading.Event()
        original_artifact = self.backup_service.export_scheduled_bundle_to_file.return_value

        def blocking_export(**_kwargs: object) -> FileBackupArtifact:
            export_started.set()
            self.assertTrue(release_export.wait(timeout=2))
            return original_artifact

        self.backup_service.export_scheduled_bundle_to_file.side_effect = blocking_export
        first = self._runner()
        second = self._runner()
        first_error: list[BaseException] = []

        def run_first() -> None:
            try:
                first.run_once()
            except BaseException as exc:  # noqa: BLE001  # pragma: no cover - surfaced below
                first_error.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(export_started.wait(timeout=1))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            second.run_once()
        release_export.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(len(self._published_archives()), 1)

    def test_post_publish_identity_race_fails_closed_and_preserves_evidence(self) -> None:
        real_link = os.link
        raced_original = self.destination / "raced-original.tar.zst.enc"

        def replace_after_link(
            source: str | os.PathLike[str],
            target: str | os.PathLike[str],
            *,
            follow_symlinks: bool = True,
        ) -> None:
            real_link(source, target, follow_symlinks=follow_symlinks)
            os.replace(target, raced_original)
            Path(target).write_bytes(b"replacement")

        runner = self._runner()
        with (
            patch("history_service.scheduled_backup.os.link", side_effect=replace_after_link),
            self.assertRaisesRegex(RuntimeError, "publication identity"),
        ):
            runner.run_once()

        archives = self._published_archives()
        self.assertEqual(len(archives), 1)
        self.assertEqual(raced_original.read_bytes(), b"authenticated-encrypted-archive")
        self.assertEqual(archives[0].read_bytes(), b"replacement")
        evidence_files = [path for path in self.destination.iterdir() if path.name.endswith(".tmp")]
        self.assertEqual(len(evidence_files), 1)
        self.assertEqual(evidence_files[0].read_bytes(), b"authenticated-encrypted-archive")
        status = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertEqual(status["last_error_code"], "RuntimeError")

    def test_post_open_path_rebind_cannot_publish_unvalidated_bytes(self) -> None:
        runner = self._runner()
        real_open = os.open
        replacement = b"unvalidated replacement"
        rebound_target: list[Path] = []

        def rebind_after_open(
            path: str | os.PathLike[str],
            flags: int,
            *args: int,
            **kwargs: int,
        ) -> int:
            descriptor = real_open(path, flags, *args, **kwargs)
            candidate = Path(path)
            if (
                candidate.parent == self.destination
                and candidate.name.startswith("jbod-scheduled-backup-")
                and not rebound_target
            ):
                candidate.unlink()
                candidate.write_bytes(replacement)
                candidate.chmod(0o600)
                rebound_target.append(candidate)
            return descriptor

        with (
            patch("history_service.scheduled_backup.os.open", side_effect=rebind_after_open),
            self.assertRaisesRegex(RuntimeError, "publication identity changed"),
        ):
            runner.run_once()

        self.assertEqual(len(rebound_target), 1)
        self.assertEqual(rebound_target[0].read_bytes(), replacement)
        self.assertTrue(list(self.destination.glob(".*.tmp")))
        status = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertEqual(status["success_count"], 0)
        self.assertEqual(status["failure_count"], 1)


class ScheduledBackupDeploymentContractTests(unittest.TestCase):
    def test_disabled_one_shot_exits_without_opening_history_or_backup_state(self) -> None:
        from history_service import scheduled_backup_main

        with (
            patch.dict(os.environ, {"SCHEDULED_BACKUP_ENABLED": "false"}, clear=False),
            patch.object(scheduled_backup_main, "configure_service_logging"),
            patch.object(scheduled_backup_main, "get_history_settings") as get_history_settings,
        ):
            self.assertEqual(scheduled_backup_main.main(), 0)

        get_history_settings.assert_not_called()

    def test_runner_module_has_no_network_server_or_docker_control_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "history_service/scheduled_backup_main.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("uvicorn", source)
        self.assertNotIn("docker", source.lower())
        self.assertNotIn("admin_service", source)
        self.assertRegex(source, re.compile(r"def main\("))


if __name__ == "__main__":
    unittest.main()
