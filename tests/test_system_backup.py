from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from app.config import PathConfig, Settings, get_settings
from app.models.domain import (
    DebugBundleExportRequest,
    DemoSystemRequest,
    SystemBackupExportRequest,
    SystemSetupBootstrapRequest,
    SystemSetupRequest,
)
from app.services.demo_system_factory import DemoSystemFactory
from app.services.ssh_key_manager import SSHKeyManager
from app.services.system_setup import PRESERVE_SECRET_SENTINEL, SystemSetupService
from history_service.config import HistorySettings
from history_service.domain import MetricSample, SlotStateRecord
from history_service.store import HistoryStore
from history_service.system_backup import (
    BACKUP_GROUP_METADATA,
    BUNDLE_FORMAT,
    BUNDLE_SCHEMA_VERSION,
    CONFIG_FILE_KEY,
    DEBUG_BUNDLE_FORMAT,
    HISTORY_DB_KEY,
    MAPPING_FILE_KEY,
    PROFILE_FILE_KEY,
    RUNTIME_OVERRIDES_FILE_KEY,
    SAS_FABRIC_ALIAS_FILE_KEY,
    SEVEN_ZIP_SIGNATURE,
    SLOT_DETAIL_FILE_KEY,
    SSH_KEYS_KEY,
    TLS_TRUST_KEY,
    KNOWN_HOSTS_KEY,
    SystemBackupService,
    _ImportActivationTransaction,
)


MARKER_ALPHA = "marker-alpha"
MARKER_BRAVO = "marker-bravo"
MARKER_CHARLIE = "marker-charlie"
MARKER_DELTA = "marker-delta"
MARKER_ECHO = "marker-echo"
MARKER_FOXTROT = "marker-foxtrot"
MARKER_GOLF = "marker-golf"
MARKER_HOTEL = "marker-hotel"
MARKER_INDIA = "marker-india"


def write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class SystemBackupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.config_path = self.temp_dir / "config.yaml"
        self.runtime_overrides_path = self.temp_dir / "runtime-overrides.yaml"
        self.profile_path = self.temp_dir / "profiles.yaml"
        self.mapping_path = self.temp_dir / "slot_mappings.json"
        self.slot_detail_path = self.temp_dir / "slot_detail_cache.json"
        self.log_path = self.temp_dir / "app.log"
        self.history_db_path = self.temp_dir / "history.db"
        self.history_backup_dir = self.temp_dir / "history-backups"
        self.ssh_dir = self.temp_dir / "ssh"
        self.tls_dir = self.temp_dir / "tls"
        self.known_hosts_path = self.temp_dir / "known_hosts"

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
        write_yaml(
            self.runtime_overrides_path,
            {"app": {"source_bundle_cache_ttl_seconds": 123}},
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
        self.ssh_dir.mkdir(parents=True, exist_ok=True)
        (self.ssh_dir / "id_truenas").write_text("PRIVATE-KEY\n", encoding="utf-8")
        (self.ssh_dir / "id_truenas.pub").write_text("ssh-ed25519 PUBLIC-KEY demo\n", encoding="utf-8")
        self.tls_dir.mkdir(parents=True, exist_ok=True)
        (self.tls_dir / "archive-core.pem").write_text(
            "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
            encoding="utf-8",
        )
        self.known_hosts_path.write_text("archive-core.local ssh-ed25519 AAAATEST\n", encoding="utf-8")

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
    def _fake_7z_passphrase_token(passphrase: str | None) -> str | None:
        if passphrase is None:
            return None
        token = hashlib.pbkdf2_hmac(
            "sha256",
            passphrase.encode("utf-8"),
            b"truenas-jbod-ui-test-fake-7z",
            100_000,
        )
        return token.hex()

    @staticmethod
    def _encode_fake_7z_archive(files: dict[str, bytes], passphrase: str | None) -> bytes:
        payload = {
            "encrypted": passphrase is not None,
            "passphrase_kdf": SystemBackupServiceTests._fake_7z_passphrase_token(passphrase),
            "files": {
                path: base64.b64encode(content).decode("ascii")
                for path, content in sorted(files.items())
            },
        }
        return SEVEN_ZIP_SIGNATURE + json.dumps(payload, sort_keys=True).encode("utf-8")

    def test_fake_7z_archive_does_not_store_cleartext_passphrase(self) -> None:
        archive_bytes = self._encode_fake_7z_archive({"manifest.json": b"{}"}, "topsecret")

        self.assertNotIn(b"topsecret", archive_bytes)
        payload = json.loads(archive_bytes[len(SEVEN_ZIP_SIGNATURE) :].decode("utf-8"))
        self.assertNotIn("passphrase", payload)
        self.assertNotIn("passphrase_sha256", payload)
        self.assertEqual(
            payload["passphrase_kdf"],
            self._fake_7z_passphrase_token("topsecret"),
        )

    @staticmethod
    def _build_zip_bundle(manifest: dict[str, object], files: dict[str, bytes] | None = None) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))
            for archive_path, content in (files or {}).items():
                archive.writestr(archive_path, content)
        return buffer.getvalue()

    @staticmethod
    def _build_selected_group_bundle(members: dict[str, bytes]) -> bytes:
        groups = []
        files = []
        archive_members = {}
        for group_key, content in members.items():
            metadata = BACKUP_GROUP_METADATA[group_key]
            archive_path = str(metadata["archive_root"])
            groups.append(
                {
                    "key": group_key,
                    "selected": True,
                    "present": True,
                    "restore_mode": metadata["restore_mode"],
                }
            )
            files.append(
                {
                    "key": group_key,
                    "group_key": group_key,
                    "archive_path": archive_path,
                    "size_bytes": len(content),
                }
            )
            archive_members[archive_path] = content
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "packaging": "zip",
            "groups": groups,
            "files": files,
        }
        return SystemBackupServiceTests._build_zip_bundle(manifest, archive_members)

    def test_directory_member_paths_reject_absolute_and_traversal_entries(self) -> None:
        invalid_paths = [
            "/tmp/outside.key",
            "C:/tmp/outside.key",
            r"C:\\tmp\\outside.key",
            r"\\\\backup-host\\share\\outside.key",
            "config/ssh/../outside.key",
            r"config\\ssh\\..\\outside.key",
            "config/ssh/subdir/../../outside.key",
            "config/ssh//outside.key",
            "config/ssh/./outside.key",
            "./outside.key",
            "config/ssh/C:/outside.key",
            "config/ssh/C:outside.key",
            "config/ssh/id_truenas:ads",
        ]

        for archive_path in invalid_paths:
            with self.subTest(archive_path=archive_path):
                with self.assertRaisesRegex(ValueError, "directory member path is invalid"):
                    SystemBackupService._directory_member_relative_path(SSH_KEYS_KEY, archive_path)

    def test_import_rejects_unsafe_manifest_archive_path_before_member_read(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "packaging": "zip",
            "groups": [
                {
                    "key": SSH_KEYS_KEY,
                    "label": "SSH Keys",
                    "archive_root": "config/ssh",
                    "selected": True,
                    "present": True,
                    "sensitive": True,
                    "restore_mode": "directory",
                }
            ],
            "files": [
                {
                    "key": "ssh-escape",
                    "group_key": SSH_KEYS_KEY,
                    "archive_path": "/tmp/outside.key",
                    "size_bytes": 3,
                }
            ],
        }
        bundle = self._build_zip_bundle(manifest, {"/tmp/outside.key": b"pwn"})

        with self.assertRaisesRegex(ValueError, "archive member path is invalid"):
            self.backup_service.import_bundle(bundle)

    def test_archive_reader_rejects_dot_and_duplicate_separator_manifest_paths(self) -> None:
        for archive_path in (
            "config/ssh//outside.key",
            "config/ssh/./outside.key",
            "./outside.key",
            "config/ssh/C:/outside.key",
            "config/ssh/C:outside.key",
            "config/ssh/id_truenas:ads",
        ):
            with self.subTest(archive_path=archive_path):
                manifest = {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "packaging": "zip",
                    "files": [
                        {
                            "key": "config_file",
                            "group_key": "config_file",
                            "archive_path": archive_path,
                            "size_bytes": 3,
                        }
                    ],
                }
                bundle = self._build_zip_bundle(manifest, {archive_path: b"pwn"})

                with self.assertRaisesRegex(ValueError, "archive member path is invalid"):
                    self.backup_service._read_archive(bundle)

    def test_directory_restore_validates_missing_members_before_replacing_existing_dir(self) -> None:
        target_dir = self.temp_dir / "existing-ssh"
        target_dir.mkdir(parents=True, exist_ok=True)
        sentinel = target_dir / "id_existing"
        sentinel.write_text("keep\n", encoding="utf-8")
        manifest = {
            "files": [
                {
                    "key": "missing-ssh-key",
                    "group_key": SSH_KEYS_KEY,
                    "archive_path": "config/ssh/missing.key",
                }
            ]
        }
        group_entries = {SSH_KEYS_KEY: {"selected": True, "present": True}}
        restored_paths: list[str] = []

        with self.assertRaisesRegex(ValueError, "missing the selected ssh_keys member"):
            self.backup_service._restore_directory_group(
                SSH_KEYS_KEY,
                manifest,
                group_entries,
                {},
                target_dir,
                restored_paths,
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(restored_paths, [])

    def test_import_rejects_missing_manifest_member_before_replacing_existing_dir(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "packaging": "zip",
            "groups": [
                {
                    "key": SSH_KEYS_KEY,
                    "label": "SSH Keys",
                    "archive_root": "config/ssh",
                    "selected": True,
                    "present": True,
                    "sensitive": True,
                    "restore_mode": "directory",
                }
            ],
            "files": [
                {
                    "key": "missing-ssh-key",
                    "group_key": SSH_KEYS_KEY,
                    "archive_path": "config/ssh/missing.key",
                    "size_bytes": 3,
                }
            ],
        }
        bundle = self._build_zip_bundle(manifest)
        existing_key = self.ssh_dir / "id_truenas"

        with self.assertRaisesRegex(ValueError, "missing config/ssh/missing.key"):
            self.backup_service.import_bundle(bundle)

        self.assertEqual(existing_key.read_text(encoding="utf-8"), "PRIVATE-KEY\n")

    def test_import_preflights_late_file_and_directory_groups_before_config_write(self) -> None:
        original_config = self.config_path.read_bytes()
        changed_config = yaml.safe_load(original_config)
        changed_config.setdefault("app", {})["refresh_interval_seconds"] = 777
        changed_config_bytes = yaml.safe_dump(changed_config, sort_keys=False).encode("utf-8")
        config_archive_path = str(BACKUP_GROUP_METADATA[CONFIG_FILE_KEY]["archive_root"])

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            for group_key in (KNOWN_HOSTS_KEY, SSH_KEYS_KEY, TLS_TRUST_KEY):
                with self.subTest(group_key=group_key):
                    manifest = {
                        "schema_version": BUNDLE_SCHEMA_VERSION,
                        "format": BUNDLE_FORMAT,
                        "packaging": "zip",
                        "groups": [
                            {
                                "key": CONFIG_FILE_KEY,
                                "selected": True,
                                "present": True,
                                "restore_mode": "file",
                            },
                            {
                                "key": group_key,
                                "selected": True,
                                "present": True,
                                "restore_mode": BACKUP_GROUP_METADATA[group_key]["restore_mode"],
                            },
                        ],
                        "files": [
                            {
                                "key": CONFIG_FILE_KEY,
                                "group_key": CONFIG_FILE_KEY,
                                "archive_path": config_archive_path,
                                "size_bytes": len(changed_config_bytes),
                                "sha256": hashlib.sha256(changed_config_bytes).hexdigest(),
                            }
                        ],
                    }
                    bundle = self._build_zip_bundle(
                        manifest,
                        {config_archive_path: changed_config_bytes},
                    )
                    get_settings.cache_clear()
                    try:
                        with self.assertRaisesRegex(ValueError, f"selected {group_key}"):
                            self.backup_service.import_bundle(bundle)
                        self.assertEqual(self.config_path.read_bytes(), original_config)
                    finally:
                        self.config_path.write_bytes(original_config)
                        get_settings.cache_clear()

    def test_import_accepts_supported_top_level_profile_list(self) -> None:
        original_profile = self.profile_path.read_bytes()
        profile_content = yaml.safe_dump(
            [
                {
                    "id": "legacy-list-profile",
                    "label": "Legacy List Profile",
                    "rows": 1,
                    "columns": 1,
                    "slot_layout": [[0]],
                }
            ],
            sort_keys=False,
        ).encode("utf-8")
        bundle = self._build_selected_group_bundle(
            {PROFILE_FILE_KEY: profile_content}
        )

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            try:
                result = self.backup_service.import_bundle(bundle)
                self.assertTrue(result["ok"])
                self.assertEqual(self.profile_path.read_bytes(), profile_content)
            finally:
                self.profile_path.write_bytes(original_profile)
                get_settings.cache_clear()

    def test_import_rejects_schema_less_sqlite_history_member(self) -> None:
        unrelated_path = self.temp_dir / "unrelated.sqlite3"
        with sqlite3.connect(unrelated_path) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            connection.commit()
        bundle = self._build_selected_group_bundle(
            {HISTORY_DB_KEY: unrelated_path.read_bytes()}
        )
        restore_dir = self.temp_dir / "history-restore"
        original_backup = self.store.create_backup(restore_dir)
        self.assertIsNotNone(original_backup)

        try:
            with self.assertRaisesRegex(ValueError, "selected history_db member is invalid"):
                self.backup_service.import_bundle(bundle)
        finally:
            assert original_backup is not None
            self.store.restore_backup(original_backup)

    def test_history_member_validation_closes_sqlite_connection(self) -> None:
        real_connect = sqlite3.connect
        opened_connections: list[sqlite3.Connection] = []

        def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection = real_connect(*args, **kwargs)
            opened_connections.append(connection)
            return connection

        with patch(
            "history_service.system_backup.sqlite3.connect",
            side_effect=tracking_connect,
        ):
            SystemBackupService._validate_history_member(self.history_db_path.read_bytes())

        self.assertEqual(len(opened_connections), 1)
        try:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
                opened_connections[0].execute("SELECT 1")
        finally:
            opened_connections[0].close()

    def test_import_rejects_duplicate_physical_zip_member_paths(self) -> None:
        content = b'{"slot_mappings": {}}'
        archive_path = str(BACKUP_GROUP_METADATA[MAPPING_FILE_KEY]["archive_root"])
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "packaging": "zip",
            "groups": [
                {
                    "key": MAPPING_FILE_KEY,
                    "selected": True,
                    "present": True,
                    "restore_mode": "file",
                }
            ],
            "files": [
                {
                    "key": MAPPING_FILE_KEY,
                    "group_key": MAPPING_FILE_KEY,
                    "archive_path": archive_path,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
                archive.writestr(archive_path, content)
                archive.writestr(archive_path, content)

        with self.assertRaisesRegex(ValueError, "duplicate physical archive member"):
            self.backup_service.import_bundle(buffer.getvalue())

    def test_import_rejects_invalid_config_before_replacing_live_config(self) -> None:
        original_config = self.config_path.read_bytes()
        bundle = self._build_selected_group_bundle(
            {CONFIG_FILE_KEY: b"systems: [\n"}
        )

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            try:
                with self.assertRaisesRegex(ValueError, "selected config_file member is invalid"):
                    self.backup_service.import_bundle(bundle)
                self.assertEqual(self.config_path.read_bytes(), original_config)
            finally:
                self.config_path.write_bytes(original_config)
                get_settings.cache_clear()

    def test_import_rejects_member_size_and_digest_mismatch_before_live_write(self) -> None:
        expected_content = b'{"slot_mappings": {}}'
        tampered_content = b'{"slot_mappings": []}'
        self.assertEqual(len(expected_content), len(tampered_content))
        archive_path = str(BACKUP_GROUP_METADATA[MAPPING_FILE_KEY]["archive_root"])
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "packaging": "zip",
            "groups": [
                {
                    "key": MAPPING_FILE_KEY,
                    "selected": True,
                    "present": True,
                    "restore_mode": "file",
                }
            ],
            "files": [
                {
                    "key": MAPPING_FILE_KEY,
                    "group_key": MAPPING_FILE_KEY,
                    "archive_path": archive_path,
                    "size_bytes": len(expected_content),
                    "sha256": hashlib.sha256(expected_content).hexdigest(),
                }
            ],
        }
        bundle = self._build_zip_bundle(manifest, {archive_path: tampered_content})
        original_mapping = self.mapping_path.read_bytes()

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
                self.backup_service.import_bundle(bundle)

        self.assertEqual(self.mapping_path.read_bytes(), original_mapping)

    def test_import_preflights_every_structured_member_before_first_live_write(self) -> None:
        original_config = self.config_path.read_bytes()
        changed_config = yaml.safe_load(original_config)
        changed_config.setdefault("app", {})["refresh_interval_seconds"] = 777
        changed_config_bytes = yaml.safe_dump(changed_config, sort_keys=False).encode("utf-8")
        invalid_members = {
            RUNTIME_OVERRIDES_FILE_KEY: b"app: [\n",
            PROFILE_FILE_KEY: b"profiles: [\n",
            MAPPING_FILE_KEY: b'{"slot_mappings":',
            SAS_FABRIC_ALIAS_FILE_KEY: b'{"aliases":',
            SLOT_DETAIL_FILE_KEY: b'{"slot_details":',
            HISTORY_DB_KEY: b"not a sqlite database",
        }
        target_paths = {
            RUNTIME_OVERRIDES_FILE_KEY: self.runtime_overrides_path,
            PROFILE_FILE_KEY: self.profile_path,
            MAPPING_FILE_KEY: self.mapping_path,
            SAS_FABRIC_ALIAS_FILE_KEY: self.temp_dir / "sas_fabric_aliases.json",
            SLOT_DETAIL_FILE_KEY: self.slot_detail_path,
            HISTORY_DB_KEY: self.history_db_path,
        }

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            for group_key, invalid_content in invalid_members.items():
                with self.subTest(group_key=group_key):
                    target_path = target_paths[group_key]
                    original_target = target_path.read_bytes() if target_path.exists() else None
                    bundle = self._build_selected_group_bundle(
                        {
                            CONFIG_FILE_KEY: changed_config_bytes,
                            group_key: invalid_content,
                        }
                    )
                    get_settings.cache_clear()
                    try:
                        with self.assertRaisesRegex(
                            ValueError,
                            f"selected {group_key} member is invalid",
                        ):
                            self.backup_service.import_bundle(bundle)
                        self.assertEqual(self.config_path.read_bytes(), original_config)
                    finally:
                        self.config_path.write_bytes(original_config)
                        if original_target is None:
                            target_path.unlink(missing_ok=True)
                        else:
                            target_path.write_bytes(original_target)
                        get_settings.cache_clear()

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
        expected_passphrase_token = payload.get("passphrase_kdf")
        archive_encrypted = bool(payload.get("encrypted"))
        supplied_passphrase_token = self._fake_7z_passphrase_token(passphrase)
        if archive_encrypted and supplied_passphrase_token != expected_passphrase_token:
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
                        group_entries = {entry["key"]: entry for entry in artifact.manifest.get("groups", [])}
                        self.assertTrue(group_entries[RUNTIME_OVERRIDES_FILE_KEY]["selected"])

                        write_yaml(self.config_path, {"default_system_id": "broken", "systems": []})
                        self.runtime_overrides_path.unlink(missing_ok=True)
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
                        self.assertEqual(restored_settings.app.source_bundle_cache_ttl_seconds, 123)
                        self.assertEqual(len(restored_settings.systems), 1)
                        restored_overrides = yaml.safe_load(
                            self.runtime_overrides_path.read_text(encoding="utf-8")
                        )
                        self.assertEqual(restored_overrides["app"]["source_bundle_cache_ttl_seconds"], 123)
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

    def test_locked_secret_paths_require_encryption(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "Encrypted export is required"):
                self.backup_service.export_bundle(
                    included_paths=[
                        "config_file",
                        "profile_file",
                        "mapping_file",
                        "slot_detail_file",
                        "history_db",
                        SSH_KEYS_KEY,
                    ],
                )

    def test_encrypted_backup_round_trip_restores_locked_secret_paths(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with patch.object(self.backup_service, "_run_7z_command", side_effect=self._fake_7z_command):
                artifact = self.backup_service.export_bundle(
                    encrypt=True,
                    passphrase="topsecret",
                    packaging="tar.zst",
                    included_paths=[
                        "config_file",
                        "profile_file",
                        "mapping_file",
                        "slot_detail_file",
                        "history_db",
                        SSH_KEYS_KEY,
                        TLS_TRUST_KEY,
                        KNOWN_HOSTS_KEY,
                    ],
                )

                group_entries = {entry["key"]: entry for entry in artifact.manifest.get("groups", [])}
                self.assertTrue(group_entries[SSH_KEYS_KEY]["selected"])
                self.assertTrue(group_entries[TLS_TRUST_KEY]["selected"])
                self.assertTrue(group_entries[KNOWN_HOSTS_KEY]["selected"])

                for file_path in self.ssh_dir.rglob("*"):
                    if file_path.is_file():
                        file_path.unlink()
                self.ssh_dir.rmdir()
                for file_path in self.tls_dir.rglob("*"):
                    if file_path.is_file():
                        file_path.unlink()
                self.tls_dir.rmdir()
                self.known_hosts_path.unlink()

                self.backup_service.import_bundle(artifact.content, passphrase="topsecret")

                self.assertEqual((self.ssh_dir / "id_truenas").read_text(encoding="utf-8"), "PRIVATE-KEY\n")
                self.assertEqual(
                    (self.tls_dir / "archive-core.pem").read_text(encoding="utf-8"),
                    "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
                )
                self.assertEqual(
                    self.known_hosts_path.read_text(encoding="utf-8"),
                    "archive-core.local ssh-ed25519 AAAATEST\n",
                )

    def test_import_rolls_back_all_live_state_when_final_validation_fails(self) -> None:
        def tree_bytes(root: Path) -> dict[str, bytes]:
            if not root.exists():
                return {}
            return {
                str(path.relative_to(root)): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

        included_paths = [
            CONFIG_FILE_KEY,
            RUNTIME_OVERRIDES_FILE_KEY,
            PROFILE_FILE_KEY,
            MAPPING_FILE_KEY,
            SAS_FABRIC_ALIAS_FILE_KEY,
            SLOT_DETAIL_FILE_KEY,
            HISTORY_DB_KEY,
            SSH_KEYS_KEY,
            TLS_TRUST_KEY,
            KNOWN_HOSTS_KEY,
        ]
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with patch.object(self.backup_service, "_run_7z_command", side_effect=self._fake_7z_command):
                artifact = self.backup_service.export_bundle(
                    encrypt=True,
                    passphrase="topsecret",
                    packaging="7z",
                    included_paths=included_paths,
                )

                live_config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
                live_config.setdefault("app", {})["refresh_interval_seconds"] = 999
                write_yaml(self.config_path, live_config)
                write_yaml(
                    self.runtime_overrides_path,
                    {"app": {"source_bundle_cache_ttl_seconds": 456}},
                )
                live_profiles = yaml.safe_load(self.profile_path.read_text(encoding="utf-8"))
                if not isinstance(live_profiles, dict):
                    raise AssertionError("Expected mapping-form profile fixture.")
                live_profile_entries = live_profiles.setdefault("profiles", [])
                live_profile_entries.append(
                    {
                        "id": "live-only-profile",
                        "label": "Live Only Profile",
                        "rows": 1,
                        "columns": 1,
                        "slot_layout": [[0]],
                    }
                )
                write_yaml(self.profile_path, live_profiles)
                sas_alias_path = self.temp_dir / "sas_fabric_aliases.json"
                self.mapping_path.write_text('{"slot_mappings": {}}\n', encoding="utf-8")
                sas_alias_path.write_text('{"sas_fabric_aliases": {}}\n', encoding="utf-8")
                self.slot_detail_path.write_text('{"slot_details": {}}\n', encoding="utf-8")
                (self.ssh_dir / "id_truenas").write_text("LIVE-PRIVATE-KEY\n", encoding="utf-8")
                (self.ssh_dir / "live-extra.pub").write_text("LIVE-PUBLIC-KEY\n", encoding="utf-8")
                (self.tls_dir / "archive-core.pem").write_text("LIVE-CERTIFICATE\n", encoding="utf-8")
                self.known_hosts_path.write_text("live-host ssh-ed25519 LIVEKEY\n", encoding="utf-8")
                self.store.insert_metric_samples(
                    [
                        MetricSample(
                            observed_at="2026-04-18T10:05:00+00:00",
                            system_id="archive-core",
                            system_label="Archive CORE",
                            enclosure_key="enc-a",
                            enclosure_id="enc-a",
                            enclosure_label="Front Shelf",
                            slot=0,
                            slot_label="00",
                            metric_name="temperature_c",
                            value_integer=37,
                            value_real=None,
                            device_name="da0",
                            serial="SERIAL-0",
                            model="Drive 0",
                            state="healthy",
                        )
                    ]
                )

                expected_files = {
                    self.config_path: self.config_path.read_bytes(),
                    self.runtime_overrides_path: self.runtime_overrides_path.read_bytes(),
                    self.profile_path: self.profile_path.read_bytes(),
                    self.mapping_path: self.mapping_path.read_bytes(),
                    sas_alias_path: sas_alias_path.read_bytes(),
                    self.slot_detail_path: self.slot_detail_path.read_bytes(),
                    self.known_hosts_path: self.known_hosts_path.read_bytes(),
                }
                expected_ssh = tree_bytes(self.ssh_dir)
                expected_tls = tree_bytes(self.tls_dir)
                expected_counts = self.store.counts()
                real_load_settings = self.backup_service._load_app_settings
                load_calls = 0

                def fail_after_history_activation():
                    nonlocal load_calls
                    load_calls += 1
                    if load_calls == 5:
                        raise RuntimeError("injected final validation failure")
                    return real_load_settings()

                with patch.object(
                    self.backup_service,
                    "_load_app_settings",
                    side_effect=fail_after_history_activation,
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected final validation failure"):
                        self.backup_service.import_bundle(
                            artifact.content,
                            passphrase="topsecret",
                        )

                self.assertEqual(load_calls, 5)
                for path, expected_content in expected_files.items():
                    with self.subTest(path=path):
                        self.assertEqual(path.read_bytes(), expected_content)
                self.assertEqual(tree_bytes(self.ssh_dir), expected_ssh)
                self.assertEqual(tree_bytes(self.tls_dir), expected_tls)
                self.assertEqual(self.store.counts(), expected_counts)

    def test_directory_activation_failure_keeps_original_tree_intact(self) -> None:
        target_dir = self.temp_dir / "transaction-directory"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "original.key").write_bytes(b"ORIGINAL")
        transaction = _ImportActivationTransaction(
            {"first": b"FIRST", "second": b"SECOND"}
        )
        real_copyfile = shutil.copyfile

        def fail_second_copy(source, destination, *args, **kwargs):
            if Path(destination).name == "second.key":
                raise OSError("injected staging write failure")
            return real_copyfile(source, destination, *args, **kwargs)

        with self.assertRaisesRegex(OSError, "injected staging write failure"):
            with transaction:
                with patch(
                    "history_service.system_backup.shutil.copyfile",
                    side_effect=fail_second_copy,
                ):
                    transaction.activate_directory(
                        target_dir,
                        [("first", Path("first.key")), ("second", Path("second.key"))],
                    )

        self.assertEqual(
            {
                str(path.relative_to(target_dir)): path.read_bytes()
                for path in target_dir.rglob("*")
                if path.is_file()
            },
            {"original.key": b"ORIGINAL"},
        )

    def test_rollback_continues_after_failure_and_preserves_recovery_material(self) -> None:
        first_path = self.temp_dir / "first-live.txt"
        second_path = self.temp_dir / "second-live.txt"
        third_path = self.temp_dir / "third-live.txt"
        first_path.write_bytes(b"FIRST-ORIGINAL")
        second_path.write_bytes(b"SECOND-ORIGINAL")
        third_path.write_bytes(b"THIRD-ORIGINAL")
        transaction = _ImportActivationTransaction(
            {
                "first": b"FIRST-IMPORTED",
                "second": b"SECOND-IMPORTED",
                "third": b"THIRD-IMPORTED",
            }
        )
        transaction_root = transaction.root
        real_restore_target = transaction._restore_target
        attempted_paths: list[Path] = []

        def fail_two_restores(entry):
            attempted_paths.append(entry.target_path)
            if entry.target_path == third_path:
                raise OSError("injected third rollback failure")
            if entry.target_path == second_path:
                raise OSError("injected second rollback failure")
            return real_restore_target(entry)

        try:
            with self.assertRaisesRegex(RuntimeError, "rollback.*incomplete") as raised:
                with patch.object(
                    transaction,
                    "_restore_target",
                    side_effect=fail_two_restores,
                ):
                    with transaction:
                        transaction.activate_file(first_path, "first")
                        transaction.activate_file(second_path, "second")
                        transaction.activate_file(third_path, "third")
                        raise ValueError("original import failure")

            self.assertEqual(attempted_paths, [third_path, second_path, first_path])
            self.assertEqual(first_path.read_bytes(), b"FIRST-ORIGINAL")
            self.assertEqual(second_path.read_bytes(), b"SECOND-IMPORTED")
            self.assertEqual(third_path.read_bytes(), b"THIRD-IMPORTED")
            self.assertIsInstance(raised.exception.__cause__, ValueError)
            self.assertRegex(str(raised.exception.__cause__), "original import failure")
            self.assertIn("injected second rollback failure", str(raised.exception))
            self.assertIn("injected third rollback failure", str(raised.exception))
            self.assertTrue(transaction_root.exists())
        finally:
            shutil.rmtree(transaction_root, ignore_errors=True)

    def test_file_target_collision_with_history_is_rejected_before_history_activation(self) -> None:
        rollback_dir = self.temp_dir / "collision-history-snapshot"
        history_snapshot = self.store.create_backup(rollback_dir, retention_count=1)
        self.assertIsNotNone(history_snapshot)
        assert history_snapshot is not None
        imported_config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        imported_config["paths"]["mapping_file"] = str(self.history_db_path)
        bundle = self._build_selected_group_bundle(
            {
                CONFIG_FILE_KEY: yaml.safe_dump(imported_config, sort_keys=False).encode("utf-8"),
                MAPPING_FILE_KEY: b'{"version": 1, "slot_mappings": {}}',
                HISTORY_DB_KEY: history_snapshot.read_bytes(),
            }
        )

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with patch.object(self.store, "restore_backup", wraps=self.store.restore_backup) as restore:
                with self.assertRaisesRegex(ValueError, "collides with the history database"):
                    self.backup_service.import_bundle(bundle)

        restore.assert_not_called()

    def test_file_target_collision_with_unselected_history_is_rejected(self) -> None:
        imported_config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        imported_config["paths"]["mapping_file"] = str(self.history_db_path)
        bundle = self._build_selected_group_bundle(
            {
                CONFIG_FILE_KEY: yaml.safe_dump(imported_config, sort_keys=False).encode("utf-8"),
                MAPPING_FILE_KEY: b'{"version": 1, "slot_mappings": {}}',
            }
        )
        expected_counts = self.store.counts()

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "collides with the history database"):
                self.backup_service.import_bundle(bundle)

        self.assertEqual(self.store.counts(), expected_counts)

    def test_symlinked_history_target_is_rejected_before_snapshot_or_activation(self) -> None:
        source_dir = self.temp_dir / "symlink-history-source"
        source_path = self.store.create_backup(source_dir, retention_count=1)
        self.assertIsNotNone(source_path)
        assert source_path is not None
        real_path = self.temp_dir / "real-history.sqlite3"
        shutil.copyfile(source_path, real_path)
        symlink_path = self.temp_dir / "symlink-history.sqlite3"
        symlink_path.symlink_to(real_path)
        symlink_store = HistoryStore(str(symlink_path))
        transaction = _ImportActivationTransaction({"history": source_path.read_bytes()})

        with patch.object(symlink_store, "create_backup", wraps=symlink_store.create_backup) as snapshot:
            with patch.object(symlink_store, "restore_backup", wraps=symlink_store.restore_backup) as restore:
                with self.assertRaisesRegex(ValueError, "symlink"):
                    with transaction:
                        transaction.activate_history(symlink_store, "history")

        snapshot.assert_not_called()
        restore.assert_not_called()
        self.assertTrue(symlink_path.is_symlink())

    def test_history_rollback_uses_store_snapshot_and_clears_sidecars(self) -> None:
        imported_source_dir = self.temp_dir / "imported-history-source"
        imported_source = self.store.create_backup(imported_source_dir, retention_count=1)
        self.assertIsNotNone(imported_source)
        assert imported_source is not None
        expected_counts = self.store.counts()
        transaction = _ImportActivationTransaction({"history": imported_source.read_bytes()})

        with patch.object(self.store, "create_backup", wraps=self.store.create_backup) as snapshot:
            with patch.object(self.store, "restore_backup", wraps=self.store.restore_backup) as restore:
                with self.assertRaisesRegex(RuntimeError, "late failure"):
                    with transaction:
                        transaction.activate_history(self.store, "history")
                        Path(f"{self.history_db_path}-wal").write_bytes(b"stale-wal")
                        Path(f"{self.history_db_path}-shm").write_bytes(b"stale-shm")
                        raise RuntimeError("late failure")

        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(restore.call_count, 2)
        self.assertEqual(self.store.counts(), expected_counts)
        self.assertFalse(Path(f"{self.history_db_path}-wal").exists())
        self.assertFalse(Path(f"{self.history_db_path}-shm").exists())

    def test_history_snapshot_fsyncs_rollback_directory_hierarchy(self) -> None:
        imported_source_dir = self.temp_dir / "durable-history-source"
        imported_source = self.store.create_backup(imported_source_dir, retention_count=1)
        self.assertIsNotNone(imported_source)
        assert imported_source is not None
        transaction = _ImportActivationTransaction({"history": imported_source.read_bytes()})
        real_fsync_directory = transaction._fsync_directory
        fsynced_paths: list[Path] = []

        def record_directory_fsync(path: Path) -> None:
            fsynced_paths.append(path)
            real_fsync_directory(path)

        with patch.object(transaction, "_fsync_directory", side_effect=record_directory_fsync):
            with transaction:
                transaction.activate_history(self.store, "history")
                transaction.commit()

        self.assertIn(transaction.rollback_root, fsynced_paths)

    def test_directory_open_fsync_failure_fails_activation_and_restores_original(self) -> None:
        target_path = self.temp_dir / "fsync-live.txt"
        target_path.write_bytes(b"ORIGINAL")
        transaction = _ImportActivationTransaction({"member": b"IMPORTED"})
        real_open = os.open
        failed = False

        def fail_first_target_directory_open(path, flags, *args, **kwargs):
            nonlocal failed
            if Path(path) == target_path.parent and not failed:
                failed = True
                raise OSError("injected directory open failure")
            return real_open(path, flags, *args, **kwargs)

        with patch("history_service.system_backup.os.open", side_effect=fail_first_target_directory_open):
            with self.assertRaisesRegex(OSError, "injected directory open failure"):
                with transaction:
                    transaction.activate_file(target_path, "member")

        self.assertTrue(failed)
        self.assertEqual(target_path.read_bytes(), b"ORIGINAL")

    def test_directory_staging_fsyncs_nested_hierarchy_before_publication(self) -> None:
        target_dir = self.temp_dir / "durable-directory"
        transaction = _ImportActivationTransaction({"member": b"KEY"})
        real_fsync_directory = transaction._fsync_directory
        fsynced_names: list[str] = []

        def record_directory_fsync(path: Path) -> None:
            fsynced_names.append(path.name)
            real_fsync_directory(path)

        with patch.object(transaction, "_fsync_directory", side_effect=record_directory_fsync):
            with transaction:
                transaction.activate_directory(
                    target_dir,
                    [("member", Path("nested/deeper/id_key"))],
                )
                transaction.commit()

        self.assertIn("deeper", fsynced_names)
        self.assertIn("nested", fsynced_names)
        self.assertTrue(any(name.startswith(f".{target_dir.name}.restore-") for name in fsynced_names))

    def test_rollback_removes_created_parent_hierarchy_and_sibling_artifacts(self) -> None:
        hierarchy_root = self.temp_dir / "new-parent"
        target_path = hierarchy_root / "nested" / "live.txt"
        transaction = _ImportActivationTransaction({"member": b"IMPORTED"})

        with self.assertRaisesRegex(RuntimeError, "late activation failure"):
            with transaction:
                transaction.activate_file(target_path, "member")
                raise RuntimeError("late activation failure")

        self.assertFalse(hierarchy_root.exists())
        self.assertEqual(list(self.temp_dir.rglob("*.restore-*")), [])
        self.assertEqual(list(self.temp_dir.rglob("*.previous-*")), [])

    def test_file_staging_descriptor_failure_removes_sibling_artifact(self) -> None:
        target_path = self.temp_dir / "descriptor-live.txt"
        target_path.write_bytes(b"ORIGINAL")
        transaction = _ImportActivationTransaction({"member": b"IMPORTED"})
        real_close = os.close
        failed = False

        def fail_first_close(descriptor: int) -> None:
            nonlocal failed
            real_close(descriptor)
            if not failed:
                failed = True
                raise OSError("injected descriptor close failure")

        with patch("history_service.system_backup.os.close", side_effect=fail_first_close):
            with self.assertRaisesRegex(OSError, "injected descriptor close failure"):
                with transaction:
                    transaction.activate_file(target_path, "member")

        self.assertTrue(failed)
        self.assertEqual(target_path.read_bytes(), b"ORIGINAL")
        self.assertEqual(list(self.temp_dir.glob(".descriptor-live.txt.restore-*")), [])

    def test_activation_preserves_modes_for_existing_file_and_directory_entries(self) -> None:
        file_target = self.temp_dir / "mode-file.txt"
        file_target.write_bytes(b"ORIGINAL")
        file_target.chmod(0o640)
        directory_target = self.temp_dir / "mode-directory"
        nested_target = directory_target / "nested"
        nested_target.mkdir(parents=True)
        directory_target.chmod(0o750)
        nested_target.chmod(0o710)
        existing_key = nested_target / "id_key"
        existing_key.write_bytes(b"ORIGINAL-KEY")
        existing_key.chmod(0o600)
        transaction = _ImportActivationTransaction(
            {"file": b"IMPORTED", "key": b"IMPORTED-KEY"}
        )

        with transaction:
            transaction.activate_file(file_target, "file")
            transaction.activate_directory(
                directory_target,
                [("key", Path("nested/id_key"))],
            )
            transaction.commit()

        self.assertEqual(file_target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(directory_target.stat().st_mode & 0o777, 0o750)
        self.assertEqual(nested_target.stat().st_mode & 0o777, 0o710)
        self.assertEqual(existing_key.stat().st_mode & 0o777, 0o600)

    def test_activation_applies_private_modes_to_missing_targets(self) -> None:
        file_target = self.temp_dir / "missing-file.txt"
        directory_target = self.temp_dir / "missing-directory"
        transaction = _ImportActivationTransaction(
            {"file": b"IMPORTED", "key": b"IMPORTED-KEY"}
        )

        with transaction:
            transaction.activate_file(file_target, "file")
            transaction.activate_directory(
                directory_target,
                [("key", Path("nested/id_key"))],
            )
            transaction.commit()

        self.assertEqual(file_target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory_target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory_target / "nested").stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory_target / "nested/id_key").stat().st_mode & 0o777, 0o600)

    def test_debug_bundle_scrubs_config_and_identifier_fields(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            artifact = self.backup_service.export_debug_bundle(
                packaging="zip",
                scrub_secrets=True,
                scrub_disk_identifiers=True,
            )

            self.assertEqual(artifact.manifest["format"], DEBUG_BUNDLE_FORMAT)
            self.assertTrue(artifact.filename.endswith(".zip"))

            with zipfile.ZipFile(io.BytesIO(artifact.content), mode="r") as archive:
                scrubbed_config = archive.read("config/config.yaml").decode("utf-8")
                scrubbed_mapping = archive.read("data/slot_mappings.json").decode("utf-8")
                debug_state = archive.read("debug/state.json").decode("utf-8")

            self.assertNotIn("API-KEY-1", scrubbed_config)
            self.assertNotIn("archive-core.local", scrubbed_config)
            self.assertNotIn("SERIAL-0", scrubbed_mapping)
            self.assertIn("REDACTED-API_KEY", scrubbed_config)
            self.assertIn("redacted-host-01.invalid", scrubbed_config)
            self.assertIn("selected_groups", debug_state)

    def test_debug_bundle_can_scrub_only_secrets(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            artifact = self.backup_service.export_debug_bundle(
                packaging="zip",
                scrub_secrets=True,
                scrub_disk_identifiers=False,
            )

            with zipfile.ZipFile(io.BytesIO(artifact.content), mode="r") as archive:
                scrubbed_config = archive.read("config/config.yaml").decode("utf-8")
                scrubbed_mapping = archive.read("data/slot_mappings.json").decode("utf-8")

            self.assertNotIn("API-KEY-1", scrubbed_config)
            self.assertIn("REDACTED-API_KEY", scrubbed_config)
            self.assertIn("SERIAL-0", scrubbed_mapping)

    def test_debug_bundle_can_scrub_only_disk_identifiers(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            artifact = self.backup_service.export_debug_bundle(
                packaging="zip",
                scrub_secrets=False,
                scrub_disk_identifiers=True,
            )

            with zipfile.ZipFile(io.BytesIO(artifact.content), mode="r") as archive:
                scrubbed_config = archive.read("config/config.yaml").decode("utf-8")
                scrubbed_mapping = archive.read("data/slot_mappings.json").decode("utf-8")

            self.assertIn("API-KEY-1", scrubbed_config)
            self.assertNotIn("SERIAL-0", scrubbed_mapping)
            self.assertIn("serial-", scrubbed_mapping)

class SecretWhitespaceModelTests(unittest.TestCase):
    def test_backup_export_request_preserves_padded_passphrase(self) -> None:
        payload = SystemBackupExportRequest(encrypt=True, passphrase="padded secret   ")

        self.assertEqual(payload.passphrase, "padded secret   ")

    def test_backup_export_request_accepts_portable_7z_packaging(self) -> None:
        payload = SystemBackupExportRequest(packaging="7z")

        self.assertEqual(payload.packaging, "7z")

    def test_backup_export_request_preserves_included_paths(self) -> None:
        payload = SystemBackupExportRequest(included_paths=["config_file", "config_file", "history_db"])

        self.assertEqual(payload.included_paths, ["config_file", "history_db"])

    def test_debug_bundle_export_request_preserves_scrub_toggle(self) -> None:
        payload = DebugBundleExportRequest(
            scrub_secrets=False,
            scrub_disk_identifiers=True,
            included_paths=["config_file", "debug_state"],
        )

        self.assertFalse(payload.scrub_secrets)
        self.assertTrue(payload.scrub_disk_identifiers)
        self.assertEqual(payload.included_paths, ["config_file", "debug_state"])

    def test_debug_bundle_export_request_aliases_legacy_scrub_toggle(self) -> None:
        payload = DebugBundleExportRequest(scrub_sensitive=False)

        self.assertFalse(payload.scrub_secrets)
        self.assertFalse(payload.scrub_disk_identifiers)

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

    def test_system_setup_request_uses_ssh_host_for_ssh_only_platforms(self) -> None:
        esxi_payload = SystemSetupRequest(
            label="CryoStorage ESXi",
            platform="esxi",
            truenas_host="",
            ssh_enabled=True,
            ssh_host="10.88.88.20",
            ssh_user="root",
        )
        linux_payload = SystemSetupRequest(
            label="GPU Server Linux",
            platform="linux",
            truenas_host="",
            ssh_enabled=True,
            ssh_host="gpu-server.local",
            ssh_user="jbodmap",
        )

        self.assertEqual(esxi_payload.truenas_host, "10.88.88.20")
        self.assertEqual(esxi_payload.ssh_host, "10.88.88.20")
        self.assertEqual(linux_payload.truenas_host, "gpu-server.local")
        self.assertEqual(linux_payload.ssh_host, "gpu-server.local")

    def test_system_setup_request_uses_bmc_host_for_ipmi_only_platform(self) -> None:
        payload = SystemSetupRequest(
            label="FatTwin Node 1",
            platform="ipmi",
            truenas_host="",
            bmc_enabled=True,
            bmc_host="10.13.0.20",
            bmc_username="ADMIN",
            bmc_password="secret",
        )

        self.assertEqual(payload.truenas_host, "10.13.0.20")
        self.assertEqual(payload.bmc_host, "10.13.0.20")

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
        self.assertIn("/usr/bin/lsscsi -g -t", saved["systems"][0]["ssh"]["commands"])
        self.assertTrue(any("lsblk --json --bytes --output" in command for command in saved["systems"][0]["ssh"]["commands"]))
        self.assertTrue(any("nvme list-subsys -o json" in command for command in saved["systems"][0]["ssh"]["commands"]))

    def test_create_system_uses_ssh_host_as_primary_host_for_esxi(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(config_path, {})

        service = SystemSetupService(str(config_path))
        created = service.create_system(
            SystemSetupRequest(
                label="CryoStorage ESXi",
                platform="esxi",
                truenas_host="",
                ssh_enabled=True,
                ssh_host="10.88.88.20",
                ssh_user="root",
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(created.truenas.host, "10.88.88.20")
        self.assertEqual(created.ssh.host, "10.88.88.20")
        self.assertEqual(saved["systems"][0]["truenas"]["host"], "10.88.88.20")
        self.assertEqual(saved["systems"][0]["ssh"]["host"], "10.88.88.20")

    def test_create_system_can_persist_password_only_ssh_without_key_path(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(config_path, {})

        service = SystemSetupService(str(config_path))
        created = service.create_system(
            SystemSetupRequest(
                label="FatTwin ESXi",
                platform="esxi",
                truenas_host="",
                ssh_enabled=True,
                ssh_host="10.13.37.121",
                ssh_user="root",
                ssh_key_path="",
                ssh_password="#EDC2wsx!QAZ",
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(created.truenas.host, "10.13.37.121")
        self.assertEqual(created.ssh.key_path, "")
        self.assertEqual(created.ssh.password, "#EDC2wsx!QAZ")
        self.assertEqual(saved["systems"][0]["ssh"]["key_path"], "")
        self.assertEqual(saved["systems"][0]["ssh"]["password"], "#EDC2wsx!QAZ")

    def test_create_system_persists_bmc_config_for_ipmi_platform(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(config_path, {})

        service = SystemSetupService(str(config_path))
        created = service.create_system(
            SystemSetupRequest(
                label="FatTwin Node 1",
                system_id="ft-node-1",
                platform="ipmi",
                truenas_host="",
                bmc_enabled=True,
                bmc_host="10.13.0.20",
                bmc_username="ADMIN",
                bmc_password="secret",
                bmc_verify_ssl=False,
                default_profile_id="supermicro-fat-twin-front-6",
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(created.truenas.host, "10.13.0.20")
        self.assertTrue(created.bmc.enabled)
        self.assertEqual(saved["systems"][0]["bmc"]["host"], "10.13.0.20")
        self.assertFalse(saved["systems"][0]["bmc"]["verify_ssl"])
        self.assertEqual(saved["systems"][0]["default_profile_id"], "supermicro-fat-twin-front-6")

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
                            "api_key": MARKER_ALPHA,
                            "api_password": MARKER_ECHO,
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
                            "password": MARKER_CHARLIE,
                            "sudo_password": MARKER_DELTA,
                            "known_hosts_path": "/app/data/known_hosts",
                            "strict_host_key_checking": True,
                            "timeout_seconds": 45,
                            "commands": ["/sbin/glabel status"],
                        },
                        "bmc": {
                            "enabled": True,
                            "host": "192.0.2.200",
                            "username": "ADMIN",
                            "password": MARKER_ECHO,
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
                api_key=MARKER_FOXTROT,
                api_password=MARKER_INDIA,
                verify_ssl=False,
                enclosure_filter="rear",
                ssh_enabled=True,
                ssh_host="archive-core-new.local",
                ssh_user="jbodmap",
                ssh_key_path="/run/ssh/id_truenas_new",
                ssh_password=MARKER_GOLF,
                ssh_sudo_password=MARKER_HOTEL,
                ssh_known_hosts_path="/app/data/known_hosts_alt",
                ssh_strict_host_key_checking=False,
                ssh_commands=["/usr/sbin/zpool status -gP"],
                bmc_enabled=True,
                bmc_host="192.0.2.201",
                bmc_username="ADMIN",
                bmc_password=MARKER_BRAVO,
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
        self.assertEqual(saved_system["truenas"]["api_key"], MARKER_FOXTROT)
        self.assertEqual(saved_system["truenas"]["api_password"], MARKER_INDIA)
        self.assertFalse(saved_system["truenas"]["verify_ssl"])
        self.assertEqual(saved_system["truenas"]["tls_ca_bundle_path"], "/app/config/tls/archive-core.pem")
        self.assertEqual(saved_system["truenas"]["tls_server_name"], "TrueNAS.gcs8.io")
        self.assertEqual(saved_system["truenas"]["timeout_seconds"], 30)
        self.assertEqual(saved_system["ssh"]["extra_hosts"], ["archive-core-backup.local"])
        self.assertEqual(saved_system["ssh"]["timeout_seconds"], 45)
        self.assertEqual(saved_system["ssh"]["key_path"], "/run/ssh/id_truenas_new")
        self.assertEqual(saved_system["ssh"]["password"], MARKER_GOLF)
        self.assertEqual(saved_system["ssh"]["sudo_password"], MARKER_HOTEL)
        self.assertEqual(saved_system["bmc"]["password"], MARKER_BRAVO)
        self.assertEqual(saved_system["enclosure_profiles"], {"enc-a": "lab-4x4"})
        self.assertEqual(saved_system["storage_views"][0]["id"], "front-bays")
        self.assertEqual(saved_system["storage_views"][0]["binding"]["enclosure_ids"], ["enc-a"])

    def test_save_system_preserves_redacted_existing_secrets(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(
            config_path,
            {
                "systems": [
                    {
                        "id": "archive-core",
                        "label": "Archive CORE",
                        "truenas": {
                            "host": "https://archive-core.local",
                            "api_key": MARKER_ALPHA,
                            "api_user": "root",
                            "api_password": MARKER_BRAVO,
                            "platform": "core",
                            "verify_ssl": True,
                        },
                        "ssh": {
                            "enabled": True,
                            "host": "archive-core.local",
                            "port": 22,
                            "user": "jbodmap",
                            "key_path": "/run/ssh/id_truenas",
                            "password": MARKER_CHARLIE,
                            "sudo_password": MARKER_DELTA,
                            "strict_host_key_checking": True,
                        },
                        "bmc": {
                            "enabled": True,
                            "host": "192.0.2.200",
                            "username": "ADMIN",
                            "password": MARKER_ECHO,
                        },
                    }
                ]
            },
        )

        service = SystemSetupService(str(config_path))
        service.save_system(
            SystemSetupRequest(
                system_id="archive-core",
                label="Archive CORE Revised",
                platform="core",
                truenas_host="https://archive-core-new.local",
                api_key=PRESERVE_SECRET_SENTINEL,
                api_user="root",
                api_password=PRESERVE_SECRET_SENTINEL,
                verify_ssl=False,
                ssh_enabled=True,
                ssh_host="archive-core-new.local",
                ssh_user="jbodmap",
                ssh_key_path="/run/ssh/id_truenas_new",
                ssh_password=PRESERVE_SECRET_SENTINEL,
                ssh_sudo_password=PRESERVE_SECRET_SENTINEL,
                bmc_enabled=True,
                bmc_host="192.0.2.201",
                bmc_username="ADMIN",
                bmc_password=PRESERVE_SECRET_SENTINEL,
                replace_existing=True,
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        saved_system = saved["systems"][0]
        self.assertEqual(saved_system["truenas"]["api_key"], MARKER_ALPHA)
        self.assertEqual(saved_system["truenas"]["api_password"], MARKER_BRAVO)
        self.assertEqual(saved_system["ssh"]["password"], MARKER_CHARLIE)
        self.assertEqual(saved_system["ssh"]["sudo_password"], MARKER_DELTA)
        self.assertEqual(saved_system["bmc"]["password"], MARKER_ECHO)

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

    def test_save_system_persists_quantastor_ha_nodes_and_targeted_storage_view(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(config_path, {})

        service = SystemSetupService(str(config_path))
        created = service.create_system(
            SystemSetupRequest(
                label="QSOSN HA",
                system_id="qsosn-ha",
                platform="quantastor",
                truenas_host="https://10.13.37.40",
                api_user="jbodmap",
                api_password="secret",
                verify_ssl=False,
                ssh_enabled=True,
                ssh_host="10.13.37.30",
                ssh_user="jbodmap",
                ha_enabled=True,
                ha_nodes=[
                    {
                        "system_id": "node-a",
                        "label": "QSOSN Left",
                        "host": "10.13.37.30",
                    },
                    {
                        "system_id": "node-b",
                        "label": "QSOSN Right",
                        "host": "10.13.37.31",
                    },
                ],
                storage_views=[
                    {
                        "id": "boot-doms-node-b",
                        "label": "Boot SATADOMs B",
                        "kind": "boot_devices",
                        "template_id": "satadom-pair-2",
                        "enabled": True,
                        "order": 30,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "hybrid",
                            "target_system_id": "node-b",
                            "pool_names": ["QSOSN-BOOT-B"],
                            "serials": ["SATADOM-B-1", "SATADOM-B-2"],
                            "device_names": ["sda", "sdb"],
                        },
                    }
                ],
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        saved_system = saved["systems"][0]
        saved_view = saved_system["storage_views"][0]

        self.assertEqual(created.id, "qsosn-ha")
        self.assertTrue(saved_system["ssh"]["ha_enabled"])
        self.assertEqual(
            saved_system["ssh"]["ha_nodes"],
            [
                {"system_id": "node-a", "label": "QSOSN Left", "host": "10.13.37.30"},
                {"system_id": "node-b", "label": "QSOSN Right", "host": "10.13.37.31"},
            ],
        )
        self.assertEqual(saved_view["binding"]["target_system_id"], "node-b")
        self.assertEqual(saved_view["binding"]["pool_names"], ["QSOSN-BOOT-B"])

    def test_save_and_reload_preserves_distinct_label_only_ha_nodes(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(config_path, {})
        service = SystemSetupService(str(config_path))

        created = service.create_system(
            SystemSetupRequest(
                label="QSOSN HA",
                system_id="qsosn-ha-labels",
                platform="quantastor",
                truenas_host="https://192.0.2.40",
                api_user="admin",
                api_password="secret",
                verify_ssl=False,
                ha_enabled=True,
                ha_nodes=[
                    {"label": "QSOSN Left"},
                    {"label": "QSOSN Right"},
                ],
            )
        )

        self.assertEqual([node.label for node in created.ssh.ha_nodes], ["QSOSN Left", "QSOSN Right"])
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(config_path)}, clear=False):
            get_settings.cache_clear()
            try:
                reloaded = get_settings()
            finally:
                get_settings.cache_clear()
        reloaded_system = next(system for system in reloaded.systems if system.id == "qsosn-ha-labels")
        self.assertEqual(
            [node.label for node in reloaded_system.ssh.ha_nodes],
            ["QSOSN Left", "QSOSN Right"],
        )

    def test_save_and_reload_preserves_explicit_blank_storage_view_label(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(config_path, {})
        service = SystemSetupService(str(config_path))

        created = service.create_system(
            SystemSetupRequest(
                label="Archive CORE",
                system_id="archive-blank-view",
                platform="core",
                truenas_host="https://192.0.2.50",
                api_key="API-KEY",
                storage_views=[
                    {
                        "id": "operator-empty-label",
                        "label": "",
                        "kind": "manual",
                        "template_id": "manual-4",
                        "enabled": True,
                        "order": 10,
                    }
                ],
            )
        )

        self.assertEqual(created.storage_views[0].label, "")
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(config_path)}, clear=False):
            get_settings.cache_clear()
            try:
                reloaded = get_settings()
            finally:
                get_settings.cache_clear()
        reloaded_system = next(system for system in reloaded.systems if system.id == "archive-blank-view")
        self.assertEqual(reloaded_system.storage_views[0].label, "")

    def test_delete_system_removes_entry_and_rehomes_default(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(
            config_path,
            {
                "default_system_id": "qs-cryostorage",
                "systems": [
                    {
                        "id": "qs-cryostorage",
                        "label": "QS CryoStorage",
                        "truenas": {
                            "host": "https://10.13.37.40",
                            "platform": "quantastor",
                        },
                        "ssh": {
                            "enabled": True,
                            "host": "10.13.37.30",
                            "extra_hosts": ["10.13.37.31"],
                            "port": 22,
                            "user": "jbodmap",
                            "key_path": "/run/ssh/id_truenas",
                            "known_hosts_path": "/app/data/known_hosts",
                            "strict_host_key_checking": True,
                            "timeout_seconds": 30,
                            "commands": [],
                        },
                    },
                    {
                        "id": "archive-core",
                        "label": "Archive CORE",
                        "truenas": {
                            "host": "https://archive-core.local",
                            "platform": "core",
                        },
                        "ssh": {
                            "enabled": True,
                            "host": "archive-core.local",
                            "port": 22,
                            "user": "jbodmap",
                            "key_path": "/run/ssh/id_truenas",
                            "known_hosts_path": "/app/data/known_hosts",
                            "strict_host_key_checking": True,
                            "timeout_seconds": 30,
                            "commands": [],
                        },
                    },
                ],
            },
        )

        service = SystemSetupService(str(config_path))
        deleted_label, next_default = service.delete_system("qs-cryostorage")

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(deleted_label, "QS CryoStorage")
        self.assertEqual(next_default, "archive-core")
        self.assertEqual(saved["default_system_id"], "archive-core")
        self.assertEqual([system["id"] for system in saved["systems"]], ["archive-core"])


class DemoSystemFactoryTests(unittest.TestCase):
    def test_create_demo_system_adds_profile_and_sample_views(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        profile_path = temp_dir / "profiles.yaml"
        write_yaml(
            config_path,
            {
                "paths": {
                    "mapping_file": str(temp_dir / "slot_mappings.json"),
                    "log_file": str(temp_dir / "app.log"),
                    "profile_file": str(profile_path),
                    "slot_detail_cache_file": str(temp_dir / "slot_detail_cache.json"),
                }
            },
        )
        write_yaml(profile_path, {"profiles": []})

        settings = Settings(
            config_file=str(config_path),
            paths=PathConfig(
                mapping_file=str(temp_dir / "slot_mappings.json"),
                log_file=str(temp_dir / "app.log"),
                profile_file=str(profile_path),
                slot_detail_cache_file=str(temp_dir / "slot_detail_cache.json"),
            ),
            systems=[],
            profiles=[],
        )
        factory = DemoSystemFactory(str(config_path), str(profile_path))

        result = factory.create_demo_system(
            DemoSystemRequest(replace_existing=True),
            settings,
        )

        saved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        saved_profiles = yaml.safe_load(profile_path.read_text(encoding="utf-8"))

        self.assertEqual(result["system"].id, "demo-builder-lab")
        self.assertEqual(result["profile"].id, "demo-builder-lab-chassis")
        self.assertEqual(saved_config["systems"][0]["default_profile_id"], "demo-builder-lab-chassis")
        self.assertEqual(len(saved_config["systems"][0]["storage_views"]), 4)
        self.assertEqual(saved_profiles["profiles"][0]["id"], "demo-builder-lab-chassis")
        self.assertEqual(saved_profiles["profiles"][0]["slot_layout"][0], [2, 5, 8, 11])


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
