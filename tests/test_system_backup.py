from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app.config import get_settings
from app.models.domain import (
    SystemBackupExportRequest,
    SystemSetupBootstrapRequest,
    SystemSetupRequest,
)
from app.services.ssh_key_manager import SSHKeyManager
from app.services.system_setup import SystemSetupService
from history_service.config import HistorySettings
from history_service.domain import MetricSample, SlotStateRecord
from history_service.store import HistoryStore
from history_service.system_backup import SEVEN_ZIP_SIGNATURE, SystemBackupService


def write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class SystemBackupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.config_path = self.temp_dir / "config.yaml"
        self.profile_path = self.temp_dir / "profiles.yaml"
        self.mapping_path = self.temp_dir / "slot_mappings.json"
        self.slot_detail_path = self.temp_dir / "slot_detail_cache.json"
        self.log_path = self.temp_dir / "app.log"
        self.history_db_path = self.temp_dir / "history.db"
        self.history_backup_dir = self.temp_dir / "history-backups"

        write_yaml(
            self.config_path,
            {
                "default_system_id": "archive-core",
                "systems": [
                    {
                        "id": "archive-core",
                        "label": "Archive CORE",
                        "default_profile_id": "supermicro-cse-946-top-60",
                        "storage_views": [
                            {
                                "id": "front-bays",
                                "label": "Front Bays",
                                "kind": "ses_enclosure",
                                "template_id": "ses-auto",
                                "profile_id": "supermicro-cse-946-top-60",
                                "enabled": True,
                                "order": 10,
                                "render": {
                                    "show_in_main_ui": True,
                                    "show_in_admin_ui": True,
                                    "default_collapsed": False,
                                },
                                "binding": {
                                    "mode": "auto",
                                    "enclosure_ids": ["enc-a"],
                                    "pool_names": [],
                                    "serials": [],
                                    "pcie_addresses": [],
                                    "device_names": [],
                                },
                            }
                        ],
                        "truenas": {
                            "host": "https://archive-core.local",
                            "api_key": "API-KEY-1",
                            "platform": "core",
                            "verify_ssl": True,
                        },
                        "ssh": {
                            "enabled": True,
                            "host": "archive-core.local",
                            "user": "jbodmap",
                            "key_path": "/run/ssh/id_truenas",
                            "known_hosts_path": "/app/data/known_hosts",
                            "strict_host_key_checking": True,
                            "commands": [
                                "/sbin/glabel status",
                                "/usr/local/sbin/zpool status -gP",
                            ],
                        },
                    }
                ],
                "paths": {
                    "mapping_file": str(self.mapping_path),
                    "log_file": str(self.log_path),
                    "profile_file": str(self.profile_path),
                    "slot_detail_cache_file": str(self.slot_detail_path),
                },
            },
        )
        write_yaml(
            self.profile_path,
            {
                "profiles": [
                    {
                        "id": "custom-lab-1x1",
                        "label": "Custom Lab 1x1",
                        "rows": 1,
                        "columns": 1,
                        "slot_layout": [[0]],
                    }
                ]
            },
        )
        self.mapping_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "slot_mappings": {
                        "archive-core:enc-a:0": {
                            "system_id": "archive-core",
                            "enclosure_id": "enc-a",
                            "slot": 0,
                            "serial": "SERIAL-0",
                            "updated_at": "2026-04-17T10:00:00+00:00",
                            "source": "manual",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.slot_detail_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "slot_details": {
                        "archive-core:enc-a:0": {
                            "system_id": "archive-core",
                            "enclosure_id": "enc-a",
                            "slot": 0,
                            "identifiers": ["SERIAL-0"],
                            "slot_fields": {"model": "Drive 0"},
                            "smart_fields": {"temperature_c": 31},
                            "updated_at": "2026-04-17T10:00:00+00:00",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        self.store = HistoryStore(str(self.history_db_path))
        self.store.upsert_slot_state(
            SlotStateRecord(
                system_id="archive-core",
                system_label="Archive CORE",
                enclosure_key="enc-a",
                enclosure_id="enc-a",
                enclosure_label="Front Shelf",
                slot=0,
                slot_label="00",
                present=True,
                state="healthy",
                identify_active=False,
                device_name="da0",
                serial="SERIAL-0",
                model="Drive 0",
                gptid="gptid/0",
                pool_name="tank",
                vdev_name="raidz2-0",
                health="ONLINE",
            ),
            "2026-04-17T10:05:00+00:00",
        )
        self.store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-17T10:05:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=0,
                    slot_label="00",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da0",
                    serial="SERIAL-0",
                    model="Drive 0",
                    state="healthy",
                )
            ]
        )
        self.backup_service = SystemBackupService(
            HistorySettings(
                sqlite_path=str(self.history_db_path),
                backup_dir=str(self.history_backup_dir),
                startup_grace_seconds=0,
            ),
            self.store,
        )

    def tearDown(self) -> None:
        get_settings.cache_clear()

    @staticmethod
    def _encode_fake_7z_archive(files: dict[str, bytes], passphrase: str | None) -> bytes:
        payload = {
            "encrypted": passphrase is not None,
            "passphrase": passphrase,
            "files": {
                path: base64.b64encode(content).decode("ascii")
                for path, content in sorted(files.items())
            },
        }
        return SEVEN_ZIP_SIGNATURE + json.dumps(payload, sort_keys=True).encode("utf-8")

    @staticmethod
    def _decode_fake_7z_archive(archive_path: Path) -> dict[str, object]:
        raw_bytes = archive_path.read_bytes()
        if not raw_bytes.startswith(SEVEN_ZIP_SIGNATURE):
            raise AssertionError("Expected fake 7z archive bytes.")
        return json.loads(raw_bytes[len(SEVEN_ZIP_SIGNATURE) :].decode("utf-8"))

    @staticmethod
    def _resolve_fake_7z_path(raw_path: str, cwd: Path | None) -> Path:
        path = Path(raw_path)
        if path.is_absolute() or cwd is None:
            return path
        return cwd / path

    def _fake_7z_command(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = args[0]
        passphrase = None
        output_dir: Path | None = None
        archive_path: Path | None = None
        members: list[str] = []

        for raw_arg in args[1:]:
            if raw_arg.startswith("-p"):
                passphrase = raw_arg[2:]
                continue
            if raw_arg.startswith("-o"):
                output_dir = self._resolve_fake_7z_path(raw_arg[2:], cwd)
                continue
            if raw_arg.startswith("-"):
                continue
            if archive_path is None:
                archive_path = self._resolve_fake_7z_path(raw_arg, cwd)
            else:
                members.append(raw_arg)

        if archive_path is None:
            raise AssertionError(f"Missing archive path for fake 7z command: {args}")

        if command == "a":
            files: dict[str, bytes] = {}
            for member_name in members:
                member_path = self._resolve_fake_7z_path(member_name, cwd)
                if member_path.is_dir():
                    for file_path in sorted(path for path in member_path.rglob("*") if path.is_file()):
                        relative_path = file_path.relative_to(cwd or member_path.parent)
                        files[str(relative_path).replace("\\", "/")] = file_path.read_bytes()
                elif member_path.is_file():
                    relative_path = member_path.relative_to(cwd or member_path.parent)
                    files[str(relative_path).replace("\\", "/")] = member_path.read_bytes()
            archive_path.write_bytes(self._encode_fake_7z_archive(files, passphrase))
            return subprocess.CompletedProcess(
                ["7z", *args],
                0,
                stdout="Everything is Ok\n",
                stderr="",
            )

        payload = self._decode_fake_7z_archive(archive_path)
        expected_passphrase = payload.get("passphrase")
        archive_encrypted = bool(payload.get("encrypted"))
        if archive_encrypted and passphrase != expected_passphrase:
            return subprocess.CompletedProcess(
                ["7z", *args],
                2,
                stdout=(
                    "ERROR: enc.7z\n"
                    "Cannot open encrypted archive. Wrong password?\n\n"
                    "ERRORS:\nHeaders Error\n"
                ),
                stderr="",
            )

        stored_files = {
            path: base64.b64decode(encoded)
            for path, encoded in dict(payload.get("files") or {}).items()
        }
        if command == "l":
            file_lines: list[str] = []
            for relative_path in sorted(stored_files):
                file_lines.extend(
                    [
                        "",
                        f"Path = {relative_path}",
                        f"Encrypted = {'+' if archive_encrypted else '-'}",
                    ]
                )
            return subprocess.CompletedProcess(
                ["7z", *args],
                0,
                stdout="\n".join(
                    [
                        f"Path = {archive_path.name}",
                        "Type = 7z",
                        f"Method = LZMA2:12{' 7zAES' if archive_encrypted else ''}",
                        *file_lines,
                    ]
                ),
                stderr="",
            )
        if command == "x":
            if output_dir is None:
                raise AssertionError(f"Missing extract directory for fake 7z command: {args}")
            output_dir.mkdir(parents=True, exist_ok=True)
            for relative_path, content in stored_files.items():
                target_path = output_dir / Path(relative_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(content)
            return subprocess.CompletedProcess(
                ["7z", *args],
                0,
                stdout="Everything is Ok\n",
                stderr="",
            )

        raise AssertionError(f"Unsupported fake 7z command: {args}")

    def test_plain_backup_round_trip_restores_config_data_and_history(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with patch.object(self.backup_service, "_run_7z_command", side_effect=self._fake_7z_command):
                for packaging, suffix, signature in (
                    ("tar.zst", ".tar.zst", b"\x28\xb5\x2f\xfd"),
                    ("zip", ".zip", b"PK"),
                    ("tar.gz", ".tar.gz", b"\x1f\x8b"),
                    ("7z", ".7z", SEVEN_ZIP_SIGNATURE),
                ):
                    with self.subTest(packaging=packaging):
                        artifact = self.backup_service.export_bundle(packaging=packaging)

                        self.assertTrue(artifact.filename.endswith(suffix))
                        self.assertTrue(artifact.content.startswith(signature))
                        self.assertEqual(artifact.manifest["packaging"], packaging)

                        write_yaml(self.config_path, {"default_system_id": "broken", "systems": []})
                        self.profile_path.unlink(missing_ok=True)
                        self.mapping_path.write_text("{}", encoding="utf-8")
                        self.slot_detail_path.write_text("{}", encoding="utf-8")
                        replacement_store = HistoryStore(str(self.history_db_path))
                        replacement_store.insert_metric_samples([])

                        result = self.backup_service.import_bundle(artifact.content)

                        restored_settings = get_settings()
                        restored_mapping = json.loads(self.mapping_path.read_text(encoding="utf-8"))
                        restored_slot_detail = json.loads(self.slot_detail_path.read_text(encoding="utf-8"))
                        counts = self.store.counts()

                        self.assertTrue(result["ok"])
                        self.assertEqual(result["packaging"], packaging)
                        self.assertEqual(restored_settings.default_system_id, "archive-core")
                        self.assertEqual(len(restored_settings.systems), 1)
                        self.assertIn("archive-core:enc-a:0", restored_mapping["slot_mappings"])
                        self.assertIn("archive-core:enc-a:0", restored_slot_detail["slot_details"])
                        self.assertEqual(counts["tracked_slots"], 1)
                        self.assertEqual(counts["metric_sample_count"], 1)

    def test_encrypted_backup_requires_correct_passphrase(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with patch.object(self.backup_service, "_run_7z_command", side_effect=self._fake_7z_command):
                artifact = self.backup_service.export_bundle(
                    encrypt=True,
                    passphrase="topsecret",
                    packaging="tar.zst",
                )

                self.assertTrue(artifact.filename.endswith(".7z"))
                self.assertTrue(artifact.content.startswith(SEVEN_ZIP_SIGNATURE))
                self.assertEqual(artifact.manifest["packaging"], "7z")

                with self.assertRaisesRegex(ValueError, "Check the passphrase"):
                    self.backup_service.import_bundle(artifact.content, passphrase="wrong-secret")
                with self.assertRaisesRegex(ValueError, "requires a passphrase"):
                    self.backup_service.import_bundle(artifact.content)

                result = self.backup_service.import_bundle(artifact.content, passphrase="topsecret")

                self.assertTrue(result["encrypted"])
                self.assertEqual(result["packaging"], "7z")
                self.assertEqual(result["system_count"], 1)

    def test_encrypted_backup_preserves_passphrase_whitespace_exactly(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            padded_passphrase = "top secret   "
            with patch.object(self.backup_service, "_run_7z_command", side_effect=self._fake_7z_command):
                artifact = self.backup_service.export_bundle(
                    encrypt=True,
                    passphrase=padded_passphrase,
                    packaging="tar.zst",
                )

                with self.assertRaisesRegex(ValueError, "Check the passphrase"):
                    self.backup_service.import_bundle(artifact.content, passphrase="top secret")

                result = self.backup_service.import_bundle(artifact.content, passphrase=padded_passphrase)

                self.assertTrue(result["encrypted"])
                self.assertEqual(result["packaging"], "7z")

class SecretWhitespaceModelTests(unittest.TestCase):
    def test_backup_export_request_preserves_padded_passphrase(self) -> None:
        payload = SystemBackupExportRequest(encrypt=True, passphrase="padded secret   ")

        self.assertEqual(payload.passphrase, "padded secret   ")

    def test_backup_export_request_accepts_portable_7z_packaging(self) -> None:
        payload = SystemBackupExportRequest(packaging="7z")

        self.assertEqual(payload.packaging, "7z")

    def test_system_setup_request_preserves_secret_whitespace(self) -> None:
        payload = SystemSetupRequest(
            label="Archive CORE",
            truenas_host="https://archive-core.local",
            api_password="api secret   ",
            ssh_enabled=True,
            ssh_user="jbodmap",
            ssh_password="ssh secret   ",
            ssh_sudo_password="sudo secret   ",
        )

        self.assertEqual(payload.api_password, "api secret   ")
        self.assertEqual(payload.ssh_password, "ssh secret   ")
        self.assertEqual(payload.ssh_sudo_password, "sudo secret   ")

    def test_bootstrap_request_preserves_secret_whitespace(self) -> None:
        payload = SystemSetupBootstrapRequest(
            host="archive-core.local",
            bootstrap_user="root",
            bootstrap_password="bootstrap secret   ",
            bootstrap_sudo_password="sudo secret   ",
            service_user="jbodmap",
            service_key_name="id_truenas",
        )

        self.assertEqual(payload.bootstrap_password, "bootstrap secret   ")
        self.assertEqual(payload.bootstrap_sudo_password, "sudo secret   ")


class SystemSetupServiceTests(unittest.TestCase):
    def test_create_system_appends_new_configured_system(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(
            config_path,
            {
                "paths": {
                    "mapping_file": str(temp_dir / "slot_mappings.json"),
                    "log_file": str(temp_dir / "app.log"),
                    "profile_file": str(temp_dir / "profiles.yaml"),
                    "slot_detail_cache_file": str(temp_dir / "slot_detail_cache.json"),
                }
            },
        )

        service = SystemSetupService(str(config_path))
        created = service.create_system(
            SystemSetupRequest(
                label="Offsite SCALE",
                platform="scale",
                truenas_host="https://scale.example.local",
                api_key="SCALE-KEY",
                ssh_enabled=True,
                ssh_user="jbodmap",
                make_default=True,
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(created.id, "offsite-scale")
        self.assertEqual(saved["default_system_id"], "offsite-scale")
        self.assertEqual(saved["systems"][0]["truenas"]["platform"], "scale")
        self.assertTrue(saved["systems"][0]["ssh"]["enabled"])
        self.assertIn("/usr/bin/lsscsi -g", saved["systems"][0]["ssh"]["commands"])

    def test_save_system_updates_existing_entry_when_replace_existing_is_true(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(
            config_path,
            {
                "default_system_id": "archive-core",
                "systems": [
                    {
                        "id": "archive-core",
                        "label": "Archive CORE",
                        "default_profile_id": "lab-4x4",
                        "storage_views": [
                            {
                                "id": "front-bays",
                                "label": "Front Bays",
                                "kind": "ses_enclosure",
                                "template_id": "ses-auto",
                                "profile_id": "lab-4x4",
                                "enabled": True,
                                "order": 10,
                                "render": {
                                    "show_in_main_ui": True,
                                    "show_in_admin_ui": True,
                                    "default_collapsed": False,
                                },
                                "binding": {
                                    "mode": "auto",
                                    "enclosure_ids": ["enc-a"],
                                    "pool_names": [],
                                    "serials": [],
                                    "pcie_addresses": [],
                                    "device_names": [],
                                },
                            }
                        ],
                        "truenas": {
                            "host": "https://archive-core.local",
                            "api_key": "OLD-KEY",
                            "platform": "core",
                            "verify_ssl": True,
                            "tls_ca_bundle_path": "/app/config/tls/archive-core.pem",
                            "tls_server_name": "TrueNAS.gcs8.io",
                            "timeout_seconds": 30,
                            "enclosure_filter": "front",
                        },
                        "ssh": {
                            "enabled": True,
                            "host": "archive-core.local",
                            "extra_hosts": ["archive-core-backup.local"],
                            "port": 22,
                            "user": "jbodmap",
                            "key_path": "/run/ssh/id_truenas",
                            "password": "old ssh password",
                            "sudo_password": "old sudo password",
                            "known_hosts_path": "/app/data/known_hosts",
                            "strict_host_key_checking": True,
                            "timeout_seconds": 45,
                            "commands": ["/sbin/glabel status"],
                        },
                        "enclosure_profiles": {"enc-a": "lab-4x4"},
                    }
                ],
            },
        )

        service = SystemSetupService(str(config_path))
        updated, replaced = service.save_system(
            SystemSetupRequest(
                system_id="archive-core",
                label="Archive CORE Revised",
                platform="core",
                truenas_host="https://archive-core-new.local",
                api_key="NEW-KEY",
                verify_ssl=False,
                enclosure_filter="rear",
                ssh_enabled=True,
                ssh_host="archive-core-new.local",
                ssh_user="jbodmap",
                ssh_key_path="/run/ssh/id_truenas_new",
                ssh_password="new ssh password",
                ssh_sudo_password="new sudo password",
                ssh_known_hosts_path="/app/data/known_hosts_alt",
                ssh_strict_host_key_checking=False,
                ssh_commands=["/usr/sbin/zpool status -gP"],
                default_profile_id="lab-2x8",
                replace_existing=True,
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        saved_system = saved["systems"][0]

        self.assertTrue(replaced)
        self.assertEqual(updated.id, "archive-core")
        self.assertEqual(len(saved["systems"]), 1)
        self.assertEqual(saved_system["label"], "Archive CORE Revised")
        self.assertEqual(saved_system["truenas"]["host"], "https://archive-core-new.local")
        self.assertFalse(saved_system["truenas"]["verify_ssl"])
        self.assertEqual(saved_system["truenas"]["tls_ca_bundle_path"], "/app/config/tls/archive-core.pem")
        self.assertEqual(saved_system["truenas"]["tls_server_name"], "TrueNAS.gcs8.io")
        self.assertEqual(saved_system["truenas"]["timeout_seconds"], 30)
        self.assertEqual(saved_system["ssh"]["extra_hosts"], ["archive-core-backup.local"])
        self.assertEqual(saved_system["ssh"]["timeout_seconds"], 45)
        self.assertEqual(saved_system["ssh"]["key_path"], "/run/ssh/id_truenas_new")
        self.assertEqual(saved_system["enclosure_profiles"], {"enc-a": "lab-4x4"})
        self.assertEqual(saved_system["storage_views"][0]["id"], "front-bays")
        self.assertEqual(saved_system["storage_views"][0]["binding"]["enclosure_ids"], ["enc-a"])

    def test_save_system_persists_explicit_storage_views(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(
            config_path,
            {
                "paths": {
                    "mapping_file": str(temp_dir / "slot_mappings.json"),
                    "log_file": str(temp_dir / "app.log"),
                    "profile_file": str(temp_dir / "profiles.yaml"),
                    "slot_detail_cache_file": str(temp_dir / "slot_detail_cache.json"),
                }
            },
        )

        service = SystemSetupService(str(config_path))
        created = service.create_system(
            SystemSetupRequest(
                label="Archive CORE",
                platform="core",
                truenas_host="https://archive-core.local",
                api_key="API-KEY",
                storage_views=[
                    {
                        "id": "front-24",
                        "label": "Front 24 Bay",
                        "kind": "ses_enclosure",
                        "template_id": "ses-auto",
                        "profile_id": "generic-front-24-1x24",
                        "enabled": True,
                        "order": 10,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "auto",
                            "enclosure_ids": [],
                            "pool_names": [],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": [],
                        },
                    },
                    {
                        "id": "hyper-m2",
                        "label": "4x NVMe Carrier Card",
                        "kind": "nvme_carrier",
                        "template_id": "nvme-carrier-4",
                        "enabled": True,
                        "order": 20,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "hybrid",
                            "pool_names": ["fast"],
                            "serials": ["SERIAL-1"],
                            "pcie_addresses": ["0000:5e:00.0"],
                        },
                        "layout_overrides": {
                            "slot_labels": {
                                0: "M2-A",
                                1: "M2-B",
                            },
                            "slot_sizes": {
                                0: "2280",
                                1: "22110",
                            },
                        },
                    }
                ],
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        saved_ses_view = saved["systems"][0]["storage_views"][0]
        saved_view = saved["systems"][0]["storage_views"][1]

        self.assertEqual(created.storage_views[0].id, "front-24")
        self.assertEqual(created.storage_views[0].profile_id, "generic-front-24-1x24")
        self.assertEqual(saved_ses_view["profile_id"], "generic-front-24-1x24")
        self.assertEqual(created.storage_views[1].id, "hyper-m2")
        self.assertEqual(saved_view["template_id"], "nvme-carrier-4")
        self.assertEqual(saved_view["binding"]["pool_names"], ["fast"])
        self.assertEqual(saved_view["binding"]["pcie_addresses"], ["0000:5e:00.0"])
        self.assertEqual(saved_view["layout_overrides"]["slot_labels"], {0: "M2-A", 1: "M2-B"})
        self.assertEqual(saved_view["layout_overrides"]["slot_sizes"], {0: "2280", 1: "22110"})


class SSHKeyManagerTests(unittest.TestCase):
    def test_generate_keypair_creates_reusable_runtime_paths(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        manager = SSHKeyManager(str(config_path))
        generated = manager.generate_keypair("Offsite Key")
        listed = manager.list_keys()

        self.assertEqual(generated["name"], "offsite-key")
        self.assertEqual(generated["runtime_private_path"], "/run/ssh/offsite-key")
        self.assertTrue(Path(generated["private_path"]).exists())
        self.assertTrue(Path(generated["public_path"]).exists())
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["fingerprint"], generated["fingerprint"])
