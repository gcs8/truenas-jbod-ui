from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import tracemalloc
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
from history_service.scheduled_backup import ScheduledBackupRunner
from history_service.config import HistorySettings
from history_service import segment_migration, system_backup as system_backup_module
from history_service.domain import MetricSample, SlotStateRecord
from history_service.migration_lock import history_write_lock
from history_service.store import SCHEMA, HistoryStore
from history_service.system_backup import (
    BACKUP_GROUP_METADATA,
    BUNDLE_FORMAT,
    BUNDLE_SCHEMA_VERSION,
    BundleMember,
    CONFIG_FILE_KEY,
    DEBUG_BUNDLE_FORMAT,
    DEBUG_README_KEY,
    DebugScrubber,
    ENCRYPTED_BACKUP_MAGIC,
    ENCRYPTED_BACKUP_NONCE_BYTES,
    ENCRYPTED_BACKUP_SALT_BYTES,
    HISTORY_DB_KEY,
    MAPPING_FILE_KEY,
    PROFILE_FILE_KEY,
    RUNTIME_OVERRIDES_FILE_KEY,
    SAS_FABRIC_ALIAS_FILE_KEY,
    SEVEN_ZIP_SIGNATURE,
    SEVEN_ZIP_TIMEOUT_SECONDS,
    SLOT_DETAIL_FILE_KEY,
    SSH_KEYS_KEY,
    TLS_TRUST_KEY,
    KNOWN_HOSTS_KEY,
    MAX_BACKUP_ARCHIVE_BYTES,
    MAX_ARCHIVE_MEMBER_COUNT,
    MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
    MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
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

    def test_blank_catalog_keeps_v1_reads_and_file_backups_working(self) -> None:
        settings = HistorySettings(
            sqlite_path=str(self.history_db_path),
            segment_catalog_path="",
            backup_dir=str(self.history_backup_dir),
            startup_grace_seconds=0,
        )
        self.assertIsNone(settings.segment_catalog_path)
        store = HistoryStore(
            settings.sqlite_path,
            recover_unreadable_database=False,
            segment_catalog_path=settings.segment_catalog_path,
        )
        self.assertEqual(store.counts()["metric_sample_count"], 1)
        service = SystemBackupService(settings, store)

        artifact = service.export_bundle_to_file(
            packaging="zip",
            included_paths=[HISTORY_DB_KEY],
        )
        try:
            self.assertEqual(artifact.manifest["schema_version"], 1)
        finally:
            artifact.cleanup()

        with patch.object(
            service,
            "_run_7z_command",
            side_effect=self._fake_7z_command,
        ):
            scheduled_artifact = service.export_scheduled_bundle_to_file(
                passphrase="hot-only scheduled backup test passphrase",
                included_paths=[HISTORY_DB_KEY],
            )
        try:
            self.assertEqual(scheduled_artifact.manifest["schema_version"], 1)
        finally:
            scheduled_artifact.cleanup()

    def _build_segmented_debug_service(
        self,
    ) -> tuple[SystemBackupService, Path, Path]:
        segmented_root = Path(
            tempfile.mkdtemp(prefix="segmented-debug-", dir=self.temp_dir)
        )
        source = segmented_root / "history.db"
        segments_directory = segmented_root / "segments"
        with sqlite3.connect(source) as connection:
            connection.executescript(SCHEMA)
            for event_id, observed_at, marker in (
                (1, "2025-01-01T00:00:00+00:00", "SEGMENT-PRIVATE-SERIAL"),
                (2, "2025-01-02T00:00:00+00:00", "HOT-PRIVATE-SERIAL"),
            ):
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        id, observed_at, system_id, enclosure_key,
                        slot, slot_label, event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        observed_at,
                        "synthetic-system",
                        "synthetic-enclosure",
                        1,
                        "slot-1",
                        "state_change",
                        json.dumps({"serial": marker}),
                    ),
                )
        segment_migration.migrate_segmented_history(
            source=source,
            segments_directory=segments_directory,
            cutoff="2025-01-02T00:00:00+00:00",
            key_id="synthetic-key-1",
            apply=True,
        )
        catalog_path = segments_directory / "catalog.json"
        settings = HistorySettings(
            sqlite_path=str(source),
            segment_catalog_path=str(catalog_path),
            backup_dir=str(segmented_root / "backups"),
            startup_grace_seconds=0,
        )
        service = SystemBackupService(
            settings,
            HistoryStore(
                str(source),
                recover_unreadable_database=False,
                segment_catalog_path=catalog_path,
            ),
        )
        return service, catalog_path, segments_directory / "segment-0001.sqlite3"

    def test_segmented_debug_export_scrubs_hot_and_segment_history(self) -> None:
        service, _catalog_path, _segment_path = self._build_segmented_debug_service()
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            artifact = service.export_debug_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY, DEBUG_README_KEY],
                scrub_secrets=False,
                scrub_disk_identifiers=True,
            )
        try:
            self.assertEqual(artifact.manifest["format"], DEBUG_BUNDLE_FORMAT)
            self.assertEqual(artifact.manifest["schema_version"], 1)
            self.assertNotIn("generation", artifact.manifest)
            self.assertNotIn("history_catalog", artifact.manifest)
            file_keys = {entry["key"] for entry in artifact.manifest["files"]}
            self.assertEqual(
                file_keys,
                {
                    HISTORY_DB_KEY,
                    DEBUG_README_KEY,
                    "history-segment-catalog",
                    "history-segment:segment-0001",
                },
            )
            with zipfile.ZipFile(artifact.path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "manifest.json",
                        "debug/README.txt",
                        "history/history.sqlite3",
                        "history/segments/catalog.json",
                        "history/segments/segment-0001.sqlite3",
                    },
                )
                self.assertIn(
                    "not a restore bundle",
                    archive.read("debug/README.txt").decode("utf-8"),
                )
                debug_catalog = json.loads(
                    archive.read("history/segments/catalog.json")
                )
                segment_content = archive.read(
                    "history/segments/segment-0001.sqlite3"
                )
                self.assertEqual(
                    debug_catalog["segments"][0]["size_bytes"],
                    len(segment_content),
                )
                self.assertEqual(
                    debug_catalog["segments"][0]["sha256"],
                    hashlib.sha256(segment_content).hexdigest(),
                )
                exported_databases = {
                    "hot": archive.read("history/history.sqlite3"),
                    "segment": segment_content,
                }
            for label, content in exported_databases.items():
                database_path = self.temp_dir / f"debug-{label}.sqlite3"
                database_path.write_bytes(content)
                with sqlite3.connect(database_path) as connection:
                    details = [row[0] for row in connection.execute("SELECT details_json FROM slot_events")]
                self.assertTrue(details, label)
                self.assertNotIn("SEGMENT-PRIVATE-SERIAL", "".join(details))
                self.assertNotIn("HOT-PRIVATE-SERIAL", "".join(details))
        finally:
            artifact.cleanup()

    def test_segmented_debug_export_supports_encrypted_packaging(self) -> None:
        service, _catalog_path, _segment_path = self._build_segmented_debug_service()
        passphrase = "synthetic segmented debug passphrase"
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(service, "_run_7z_command", side_effect=self._fake_7z_command),
        ):
            get_settings.cache_clear()
            artifact = service.export_debug_bundle_to_file(
                encrypt=True,
                passphrase=passphrase,
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
                scrub_secrets=False,
                scrub_disk_identifiers=False,
            )
        try:
            payload = json.loads(
                artifact.path.read_bytes()[len(SEVEN_ZIP_SIGNATURE) :].decode("utf-8")
            )
            self.assertTrue(payload["encrypted"])
            self.assertEqual(payload["passphrase_kdf"], self._fake_7z_passphrase_token(passphrase))
            self.assertEqual(
                set(payload["files"]),
                {
                    "manifest.json",
                    "history/history.sqlite3",
                    "history/segments/catalog.json",
                    "history/segments/segment-0001.sqlite3",
                },
            )
        finally:
            artifact.cleanup()

    def test_scrubbed_history_snapshot_removes_deleted_identifier_residue(self) -> None:
        service, _catalog_path, _segment_path = self._build_segmented_debug_service()
        source_path = Path(service.history_settings.sqlite_path)
        private_nonce = b"ZXQ9PRIVATE7MKR-FREELIST-6c24f8"
        with sqlite3.connect(source_path) as connection:
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("CREATE TABLE deleted_private_residue(value BLOB)")
            connection.executemany(
                "INSERT INTO deleted_private_residue(value) VALUES (?)",
                [(private_nonce + (b"X" * 3000),)] * 32,
            )
            connection.commit()
            connection.execute("DROP TABLE deleted_private_residue")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertGreater(connection.execute("PRAGMA freelist_count").fetchone()[0], 0)

        self.assertTrue(
            private_nonce in source_path.read_bytes(),
            "synthetic source must retain deleted identifier bytes before scrubbing",
        )
        scrubbed_path = self.temp_dir / "scrubbed-deleted-residue.sqlite3"

        service._build_scrubbed_history_snapshot_file(
            source_path,
            DebugScrubber(scrub_secrets=True, scrub_disk_identifiers=True),
            scrubbed_path,
        )

        self.assertFalse(
            private_nonce in scrubbed_path.read_bytes(),
            "scrubbed debug database must not retain deleted identifier bytes",
        )

    def test_segmented_snapshot_rejects_unreferenced_segment_file(self) -> None:
        service, catalog_path, segment_path = self._build_segmented_debug_service()
        orphan_path = catalog_path.parent / "segment-orphan.sqlite3"
        shutil.copyfile(segment_path, orphan_path)

        with self.assertRaisesRegex(ValueError, "unreferenced"):
            service._build_segmented_history_snapshot_to_directory(
                self.temp_dir / "unreferenced-segment-snapshot"
            )

    def test_segmented_snapshot_rejects_catalog_change_during_copy(self) -> None:
        service, catalog_path, segment_path = self._build_segmented_debug_service()
        real_copyfile = shutil.copyfile

        def copy_then_change_catalog(
            source_path: str | Path,
            destination_path: str | Path,
            *args: Any,
            **kwargs: Any,
        ) -> str:
            copied = str(real_copyfile(source_path, destination_path, *args, **kwargs))
            if Path(source_path) == segment_path:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                catalog["generation_id"] = "generation-changed"
                catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            return copied

        with (
            patch.object(system_backup_module.shutil, "copyfile", side_effect=copy_then_change_catalog),
            self.assertRaisesRegex(ValueError, "catalog changed"),
        ):
            service._build_segmented_history_snapshot_to_directory(
                self.temp_dir / "changed-catalog-snapshot"
            )

    def test_segmented_history_export_emits_schema_v2_hot_and_segment_members(self) -> None:
        segmented_root = self.temp_dir / "segmented-export"
        segmented_root.mkdir()
        source = segmented_root / "history.db"
        segments_directory = segmented_root / "segments"
        with sqlite3.connect(source) as connection:
            connection.executescript(SCHEMA)
            for event_id, observed_at in (
                (1, "2025-01-01T00:00:00+00:00"),
                (2, "2025-01-02T00:00:00+00:00"),
            ):
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        id, observed_at, system_id, enclosure_key,
                        slot, slot_label, event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        observed_at,
                        "archive-core",
                        "enc-a",
                        1,
                        "slot-1",
                        "state_change",
                        "{}",
                    ),
                )
        segment_migration.migrate_segmented_history(
            source=source,
            segments_directory=segments_directory,
            cutoff="2025-01-02T00:00:00+00:00",
            key_id="test-key-1",
            apply=True,
        )
        catalog_path = segments_directory / "catalog.json"
        history_settings = HistorySettings(
            sqlite_path=str(source),
            segment_catalog_path=str(catalog_path),
            backup_dir=str(segmented_root / "backups"),
            startup_grace_seconds=0,
        )
        service = SystemBackupService(
            history_settings,
            HistoryStore(
                str(source),
                recover_unreadable_database=False,
                segment_catalog_path=catalog_path,
            ),
        )

        artifact = service.export_bundle_to_file(
            encrypt=False,
            packaging="zip",
            included_paths=[HISTORY_DB_KEY],
        )
        try:
            self.assertEqual(artifact.manifest["schema_version"], 2)
            self.assertTrue(artifact.manifest["generation"]["complete"])
            self.assertEqual(
                [segment["segment_id"] for segment in artifact.manifest["history_catalog"]["segments"]],
                ["segment-0001"],
            )
            file_keys = {entry["key"] for entry in artifact.manifest["files"]}
            self.assertEqual(
                file_keys,
                {HISTORY_DB_KEY, "history-segment:segment-0001"},
            )
            with zipfile.ZipFile(artifact.path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "manifest.json",
                        "history/history.sqlite3",
                        "history/segments/segment-0001.sqlite3",
                    },
                )
            target_root = self.temp_dir / "segmented-import"
            target_root.mkdir()
            target_source = target_root / "history.db"
            with sqlite3.connect(target_source) as connection:
                connection.executescript(SCHEMA)
            target_catalog = target_root / "segments" / "catalog.json"
            target_settings = HistorySettings(
                sqlite_path=str(target_source),
                segment_catalog_path=str(target_catalog),
                backup_dir=str(target_root / "backups"),
                startup_grace_seconds=0,
            )
            target_store = HistoryStore(
                str(target_source),
                recover_unreadable_database=False,
                segment_catalog_path=target_catalog,
            )
            target_service = SystemBackupService(target_settings, target_store)
            original_target_bytes = target_source.read_bytes()
            original_activate_prepared_target = (
                _ImportActivationTransaction._activate_prepared_target
            )
            activation_count = 0

            def fail_before_segment_activation(transaction, *args, **kwargs) -> None:
                nonlocal activation_count
                activation_count += 1
                if activation_count == 2:
                    raise RuntimeError("injected segment activation failure")
                original_activate_prepared_target(transaction, *args, **kwargs)

            with patch.object(
                _ImportActivationTransaction,
                "_activate_prepared_target",
                autospec=True,
                side_effect=fail_before_segment_activation,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected segment activation failure"):
                    target_service.import_bundle(artifact.path.read_bytes())

            self.assertEqual(target_source.read_bytes(), original_target_bytes)
            self.assertFalse(target_catalog.exists())
            self.assertFalse((target_catalog.parent / "segment-0001.sqlite3").exists())

            result = target_service.import_bundle(artifact.path.read_bytes())

            self.assertEqual(result["schema_version"], 2)
            self.assertTrue(target_catalog.is_file())
            self.assertTrue((target_catalog.parent / "segment-0001.sqlite3").is_file())
            self.assertEqual(
                [event["id"] for event in target_store.list_slot_events("archive-core", "enc-a", 1)],
                [2, 1],
            )
            with sqlite3.connect(source) as connection:
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        id, observed_at, system_id, enclosure_key,
                        slot, slot_label, event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        3,
                        "2025-01-03T00:00:00+00:00",
                        "archive-core",
                        "enc-a",
                        1,
                        "slot-1",
                        "state_change",
                        "{}",
                    ),
                )
            successor_artifact = service.export_bundle_to_file(
                encrypt=False,
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            visible_during_activation: list[list[int]] = []
            read_errors: list[str] = []
            split_activation_count = 0

            def reject_segment_directory(transaction, *args: Any, **kwargs: Any) -> None:
                nonlocal split_activation_count
                split_activation_count += 1
                if split_activation_count == 1:
                    original_activate_prepared_target(transaction, *args, **kwargs)
                    return
                try:
                    visible_during_activation.append(
                        [
                            event["id"]
                            for event in target_store.list_slot_events(
                                "archive-core",
                                "enc-a",
                                1,
                            )
                        ]
                    )
                except ValueError as exc:
                    read_errors.append(str(exc))
                raise RuntimeError("injected split-generation activation failure")

            try:
                with patch.object(
                    _ImportActivationTransaction,
                    "_activate_prepared_target",
                    autospec=True,
                    side_effect=reject_segment_directory,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "injected split-generation activation failure",
                    ):
                        target_service.import_bundle(successor_artifact.path.read_bytes())
            finally:
                successor_artifact.cleanup()

            self.assertEqual(visible_during_activation, [])
            self.assertTrue(any("activation" in error.lower() for error in read_errors))
            self.assertEqual(
                [event["id"] for event in target_store.list_slot_events("archive-core", "enc-a", 1)],
                [2, 1],
            )
            with (
                patch.object(
                    service,
                    "_run_7z_command",
                    side_effect=self._fake_7z_command,
                ),
                patch.object(
                    target_service,
                    "_run_7z_command",
                    side_effect=self._fake_7z_command,
                ),
            ):
                encrypted_artifact = service.export_scheduled_bundle_to_file(
                    passphrase="segmented round trip passphrase",
                    included_paths=[HISTORY_DB_KEY],
                )
                try:
                    encrypted_result = target_service.import_bundle_from_file(
                        encrypted_artifact.path,
                        passphrase="segmented round trip passphrase",
                    )
                    self.assertEqual(encrypted_result["schema_version"], 2)
                    self.assertTrue(encrypted_result["encrypted"])
                    self.assertEqual(
                        [
                            event["id"]
                            for event in target_store.list_slot_events(
                                "archive-core",
                                "enc-a",
                                1,
                            )
                        ],
                        [3, 2, 1],
                    )
                finally:
                    encrypted_artifact.cleanup()
        finally:
            artifact.cleanup()

    def test_segmented_history_export_releases_write_lock_before_segment_copy(self) -> None:
        segmented_root = self.temp_dir / "segmented-export-lock"
        segmented_root.mkdir()
        source = segmented_root / "history.db"
        segments_directory = segmented_root / "segments"
        with sqlite3.connect(source) as connection:
            connection.executescript(SCHEMA)
            for event_id, observed_at in (
                (1, "2025-01-01T00:00:00+00:00"),
                (2, "2025-01-02T00:00:00+00:00"),
            ):
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        id, observed_at, system_id, enclosure_key,
                        slot, slot_label, event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, observed_at, "system-1", "enc-1", 1, "slot-1", "state_change", "{}"),
                )
        segment_migration.migrate_segmented_history(
            source=source,
            segments_directory=segments_directory,
            cutoff="2025-01-02T00:00:00+00:00",
            key_id="test-key-1",
            apply=True,
        )
        catalog_path = segments_directory / "catalog.json"
        store = HistoryStore(
            str(source),
            recover_unreadable_database=False,
            segment_catalog_path=catalog_path,
        )
        service = SystemBackupService(
            HistorySettings(
                sqlite_path=str(source),
                segment_catalog_path=str(catalog_path),
                backup_dir=str(segmented_root / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        segment_path = segments_directory / "segment-0001.sqlite3"
        expected_segment_bytes = segment_path.read_bytes()
        real_copyfile = shutil.copyfile
        copied_without_write_lock = False

        def copy_after_lock_release(
            source_path: str | Path,
            destination_path: str | Path,
            *args: Any,
            **kwargs: Any,
        ) -> str:
            nonlocal copied_without_write_lock
            if Path(source_path) == segment_path:
                with history_write_lock(source, blocking=False):
                    copied_without_write_lock = True
            return str(real_copyfile(source_path, destination_path, *args, **kwargs))

        with patch.object(system_backup_module.shutil, "copyfile", side_effect=copy_after_lock_release):
            snapshot = service._build_segmented_history_snapshot_to_directory(
                segmented_root / "snapshot"
            )

        self.assertTrue(copied_without_write_lock)
        self.assertEqual(snapshot.segment_paths[0].read_bytes(), expected_segment_bytes)

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

    def test_native_encrypted_scheduled_backup_restores_selected_state_without_7z_arguments(
        self,
    ) -> None:
        original_mapping = self.mapping_path.read_bytes()
        original_profile = self.profile_path.read_bytes()
        original_slot_detail = self.slot_detail_path.read_bytes()
        passphrase = "scheduled backup test passphrase"

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=AssertionError("scheduled encryption must not invoke 7z"),
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_scheduled_bundle_to_file(
                passphrase=passphrase,
                included_paths=[MAPPING_FILE_KEY, PROFILE_FILE_KEY, SLOT_DETAIL_FILE_KEY],
            )
            try:
                encrypted = artifact.path.read_bytes()
                self.assertTrue(encrypted.startswith(ENCRYPTED_BACKUP_MAGIC))
                self.assertNotIn(passphrase.encode("utf-8"), encrypted)

                self.mapping_path.write_text('{"version": 1, "slot_mappings": {}}', encoding="utf-8")
                write_yaml(self.profile_path, {"profiles": []})
                self.slot_detail_path.write_text(
                    '{"version": 1, "slot_details": {}}',
                    encoding="utf-8",
                )

                result = self.backup_service.import_bundle(encrypted, passphrase=passphrase)
            finally:
                artifact.cleanup()

        self.assertEqual(self.mapping_path.read_bytes(), original_mapping)
        self.assertEqual(self.profile_path.read_bytes(), original_profile)
        self.assertEqual(self.slot_detail_path.read_bytes(), original_slot_detail)
        self.assertEqual(
            set(result["included_groups"]),
            {MAPPING_FILE_KEY, PROFILE_FILE_KEY, SLOT_DETAIL_FILE_KEY},
        )

    def test_native_encrypted_scheduled_backup_rejects_wrong_passphrase_before_activation(
        self,
    ) -> None:
        original_mapping = self.mapping_path.read_bytes()
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            artifact = self.backup_service.export_scheduled_bundle_to_file(
                passphrase="correct passphrase",
                included_paths=[MAPPING_FILE_KEY],
            )
            try:
                with self.assertRaisesRegex(ValueError, "could not be decrypted"):
                    self.backup_service.import_bundle(
                        artifact.path.read_bytes(),
                        passphrase="wrong passphrase",
                    )
            finally:
                artifact.cleanup()

        self.assertEqual(self.mapping_path.read_bytes(), original_mapping)

    def test_native_scheduled_encryption_randomizes_and_authenticates_the_envelope(self) -> None:
        passphrase = "scheduled envelope authentication passphrase"
        selected_groups = [MAPPING_FILE_KEY]
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            first = self.backup_service.export_scheduled_bundle_to_file(
                passphrase=passphrase,
                included_paths=selected_groups,
            )
            second = self.backup_service.export_scheduled_bundle_to_file(
                passphrase=passphrase,
                included_paths=selected_groups,
            )
            try:
                first_bytes = first.path.read_bytes()
                second_bytes = second.path.read_bytes()
                random_start = len(ENCRYPTED_BACKUP_MAGIC)
                random_end = random_start + ENCRYPTED_BACKUP_SALT_BYTES + ENCRYPTED_BACKUP_NONCE_BYTES
                self.assertNotEqual(first_bytes, second_bytes)
                self.assertNotEqual(first_bytes[random_start:random_end], second_bytes[random_start:random_end])

                for index in (0, len(first_bytes) - 1):
                    tampered = bytearray(first_bytes)
                    tampered[index] ^= 1
                    tampered_path = self.temp_dir / f"tampered-scheduled-{index}.enc"
                    tampered_path.write_bytes(tampered)
                    with self.assertRaises(ValueError):
                        self.backup_service.preflight_scheduled_bundle_file(
                            tampered_path,
                            passphrase=passphrase,
                            expected_groups=selected_groups,
                        )
            finally:
                first.cleanup()
                second.cleanup()

    def test_native_encrypted_schedule_can_include_locked_secret_groups_without_7z(
        self,
    ) -> None:
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=AssertionError("scheduled encryption must not invoke 7z"),
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_scheduled_bundle_to_file(
                passphrase="scheduled secret group passphrase",
                included_paths=[SSH_KEYS_KEY, TLS_TRUST_KEY, KNOWN_HOSTS_KEY],
            )
            try:
                group_entries = {
                    entry["key"]: entry for entry in artifact.manifest.get("groups", [])
                }
                self.assertTrue(group_entries[SSH_KEYS_KEY]["selected"])
                self.assertTrue(group_entries[TLS_TRUST_KEY]["selected"])
                self.assertTrue(group_entries[KNOWN_HOSTS_KEY]["selected"])
            finally:
                artifact.cleanup()

    def test_public_file_export_has_no_plaintext_outer_envelope_bypass(self) -> None:
        artifact = None
        try:
            with self.assertRaises(TypeError):
                artifact = self.backup_service.export_bundle_to_file(
                    encrypt=False,
                    packaging="tar.zst",
                    included_paths=[SSH_KEYS_KEY],
                    _encrypted_outer_envelope=True,
                )
        finally:
            if artifact is not None:
                artifact.cleanup()

    def test_scheduled_backup_public_preflight_validates_copy_and_exact_group_contract(
        self,
    ) -> None:
        passphrase = "scheduled public preflight passphrase"
        selected_groups = [HISTORY_DB_KEY]
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch("history_service.system_backup.MAX_BACKUP_ARCHIVE_BYTES", 64),
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=self._fake_7z_command,
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_scheduled_bundle_to_file(
                passphrase=passphrase,
                included_paths=selected_groups,
            )
            self.assertGreater(artifact.path.stat().st_size, 64)
            copied = self.temp_dir / "copied-scheduled-backup.7z"
            copied.write_bytes(artifact.path.read_bytes())
            try:
                result = self.backup_service.preflight_scheduled_bundle_file(
                    copied,
                    passphrase=passphrase,
                    expected_groups=selected_groups,
                )
                self.assertEqual(set(result["selected_groups"]), set(selected_groups))
                self.assertEqual(result["absent_groups"], [])

                with self.assertRaisesRegex(ValueError, "do not match"):
                    self.backup_service.preflight_scheduled_bundle_file(
                        copied,
                        passphrase=passphrase,
                        expected_groups=[MAPPING_FILE_KEY],
                    )

                tampered = bytearray(copied.read_bytes())
                tampered[len(tampered) // 2] ^= 1
                copied.write_bytes(tampered)
                with self.assertRaises(ValueError):
                    self.backup_service.preflight_scheduled_bundle_file(
                        copied,
                        passphrase=passphrase,
                        expected_groups=selected_groups,
                    )
            finally:
                artifact.cleanup()

    def test_one_shot_runner_publishes_restore_grade_mapping_profile_and_calibration_backup(
        self,
    ) -> None:
        original_mapping = self.mapping_path.read_bytes()
        original_profile = self.profile_path.read_bytes()
        original_slot_detail = self.slot_detail_path.read_bytes()
        destination = self.temp_dir / "scheduled-destination"
        status_dir = self.temp_dir / "scheduled-status"
        status_dir.mkdir(mode=0o2750)
        status_dir.chmod(0o2750)
        status_file = status_dir / "status.json"
        passphrase_file = self.temp_dir / "scheduled-passphrase"
        passphrase_file.write_text("end to end scheduled passphrase\n", encoding="utf-8")
        passphrase_file.chmod(0o600)
        selected_groups = [MAPPING_FILE_KEY, PROFILE_FILE_KEY, SLOT_DETAIL_FILE_KEY]
        runner = ScheduledBackupRunner(
            self.backup_service,
            destination_dir=destination,
            status_file=status_file,
            passphrase_file=passphrase_file,
            included_groups=selected_groups,
            retention_count=3,
            app_gid=status_dir.stat().st_gid,
        )

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=AssertionError("state-only scheduled backup must not invoke 7z"),
            ),
        ):
            get_settings.cache_clear()
            status = runner.run_once()
            archives = list(destination.glob("jbod-scheduled-backup-*.tar.zst.enc"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(status["success_count"], 1)
            self.assertEqual(status["last_absent_groups"], [])

            self.mapping_path.write_text('{"version": 1, "slot_mappings": {}}', encoding="utf-8")
            write_yaml(self.profile_path, {"profiles": []})
            self.slot_detail_path.write_text(
                '{"version": 1, "slot_details": {}}',
                encoding="utf-8",
            )
            self.backup_service.import_bundle(
                archives[0].read_bytes(),
                passphrase="end to end scheduled passphrase",
            )

        self.assertEqual(self.mapping_path.read_bytes(), original_mapping)
        self.assertEqual(self.profile_path.read_bytes(), original_profile)
        self.assertEqual(self.slot_detail_path.read_bytes(), original_slot_detail)

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
                    "archive_root": archive_path,
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

    def test_import_rejects_oversized_archive_before_format_processing(self) -> None:
        with patch("history_service.system_backup.MAX_BACKUP_ARCHIVE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "archive exceeds"):
                self.backup_service.import_bundle(b"PK123")

    def test_encrypted_file_import_can_exceed_in_memory_archive_limit(self) -> None:
        passphrase = "file backed archive passphrase"
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch("history_service.system_backup.MAX_BACKUP_ARCHIVE_BYTES", 64),
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=self._fake_7z_command,
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_bundle_to_file(
                encrypt=True,
                passphrase=passphrase,
                included_paths=[MAPPING_FILE_KEY],
            )
            try:
                self.assertGreater(artifact.path.stat().st_size, 64)
                with self.assertRaisesRegex(ValueError, "file-backed export"):
                    self.backup_service.export_bundle(
                        encrypt=True,
                        passphrase=passphrase,
                        included_paths=[MAPPING_FILE_KEY],
                    )
                with self.assertRaisesRegex(ValueError, "archive exceeds"):
                    self.backup_service.import_bundle(
                        artifact.path.read_bytes(),
                        passphrase=passphrase,
                    )

                result = self.backup_service.import_bundle_from_file(
                    artifact.path,
                    passphrase=passphrase,
                )

                self.assertEqual(result["schema_version"], 1)
                self.assertTrue(result["encrypted"])
                self.assertIn(MAPPING_FILE_KEY, result["included_groups"])
                self.assertTrue(result["restored_paths"])
            finally:
                artifact.cleanup()

    def test_zip_member_limit_rejects_before_zipfile_construction(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        bundle = self._build_zip_bundle(
            manifest,
            {f"ignored-{index}.txt": b"" for index in range(3)},
        )

        with (
            patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_COUNT", 3),
            patch(
                "history_service.system_backup.zipfile.ZipFile",
                side_effect=AssertionError("ZipFile must not run before member-count rejection"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "too many members"):
                self.backup_service.import_bundle(bundle)

    def test_zip_member_limit_counts_directory_records_instead_of_trusting_footer(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        bundle = bytearray(
            self._build_zip_bundle(
                manifest,
                {f"ignored-{index}.txt": b"" for index in range(3)},
            )
        )
        footer_offset = bundle.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(footer_offset, 0)
        struct.pack_into("<HH", bundle, footer_offset + 8, 1, 1)

        with (
            patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_COUNT", 3),
            patch(
                "history_service.system_backup.zipfile.ZipFile",
                side_effect=AssertionError("ZipFile must not run before member-count rejection"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "too many members"):
                self.backup_service.import_bundle(bytes(bundle))

    def test_zip_rejects_later_eocd_signature_hidden_in_footer_comment(self) -> None:
        bundle = bytearray(
            self._build_zip_bundle(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "groups": [],
                    "files": [],
                }
            )
        )
        footer_offset = bundle.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(footer_offset, 0)
        hidden_footer = struct.pack(
            "<4s4H2IH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            0,
            0,
            0,
        )
        comment = b"prefix" + hidden_footer + b"trailing"
        bundle.extend(comment)
        struct.pack_into("<H", bundle, footer_offset + 20, len(comment))

        with patch(
            "history_service.system_backup.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not resolve a different EOCD footer"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid or ambiguous directory footer"):
                self.backup_service.import_bundle(bytes(bundle))

    def test_zip_compression_ratio_counts_ignored_members(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        bundle = self._build_zip_bundle(manifest, {"ignored-bomb.bin": b"0" * 4096})

        with patch("history_service.system_backup.MAX_ARCHIVE_COMPRESSION_RATIO", 2):
            with self.assertRaisesRegex(ValueError, "compression ratio"):
                self.backup_service.import_bundle(bundle)

    def test_zip_rejects_unsupported_compression_before_zipfile_construction(self) -> None:
        bundle = bytearray(
            self._build_zip_bundle(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "groups": [],
                    "files": [],
                }
            )
        )
        local_offset = bundle.find(b"PK\x03\x04")
        central_offset = bundle.find(b"PK\x01\x02")
        self.assertGreaterEqual(local_offset, 0)
        self.assertGreaterEqual(central_offset, 0)
        struct.pack_into("<H", bundle, local_offset + 8, 99)
        struct.pack_into("<H", bundle, central_offset + 10, 99)

        with patch(
            "history_service.system_backup.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not run for unsupported compression"),
        ):
            with self.assertRaisesRegex(ValueError, "compression method"):
                self.backup_service.import_bundle(bytes(bundle))

    def test_zip_rejects_encryption_flags_before_zipfile_construction(self) -> None:
        bundle = bytearray(
            self._build_zip_bundle(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "groups": [],
                    "files": [],
                }
            )
        )
        local_offset = bundle.find(b"PK\x03\x04")
        central_offset = bundle.find(b"PK\x01\x02")
        self.assertGreaterEqual(local_offset, 0)
        self.assertGreaterEqual(central_offset, 0)
        struct.pack_into("<H", bundle, local_offset + 6, 1)
        struct.pack_into("<H", bundle, central_offset + 8, 1)

        with patch(
            "history_service.system_backup.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not run for encrypted ZIP input"),
        ):
            with self.assertRaisesRegex(ValueError, "encryption"):
                self.backup_service.import_bundle(bytes(bundle))

    def test_zip_rejects_local_zip64_size_sentinels_before_zipfile_construction(self) -> None:
        bundle = bytearray(
            self._build_zip_bundle(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "groups": [],
                    "files": [],
                }
            )
        )
        local_offset = bundle.find(b"PK\x03\x04")
        self.assertGreaterEqual(local_offset, 0)
        struct.pack_into("<II", bundle, local_offset + 18, 0xFFFFFFFF, 0xFFFFFFFF)

        with patch(
            "history_service.system_backup.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not run for local ZIP64 metadata"),
        ):
            with self.assertRaisesRegex(ValueError, "ZIP64"):
                self.backup_service.import_bundle(bytes(bundle))

    def test_zip_rejects_local_crc_mismatch_before_zipfile_construction(self) -> None:
        bundle = bytearray(
            self._build_zip_bundle(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "groups": [],
                    "files": [],
                }
            )
        )
        local_offset = bundle.find(b"PK\x03\x04")
        self.assertGreaterEqual(local_offset, 0)
        original_crc = struct.unpack_from("<I", bundle, local_offset + 14)[0]
        struct.pack_into("<I", bundle, local_offset + 14, original_crc ^ 0xFFFFFFFF)

        with patch(
            "history_service.system_backup.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not run for inconsistent local metadata"),
        ):
            with self.assertRaisesRegex(ValueError, "local header is inconsistent"):
                self.backup_service.import_bundle(bytes(bundle))

    def test_zip_rejects_local_zip64_extra_before_zipfile_construction(self) -> None:
        bundle = bytearray(
            self._build_zip_bundle(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "groups": [],
                    "files": [],
                }
            )
        )
        local_offset = bundle.find(b"PK\x03\x04")
        central_offset = bundle.find(b"PK\x01\x02")
        self.assertGreaterEqual(local_offset, 0)
        self.assertGreaterEqual(central_offset, 0)
        local_filename_size = struct.unpack_from("<H", bundle, local_offset + 26)[0]
        local_extra_size = struct.unpack_from("<H", bundle, local_offset + 28)[0]
        zip64_extra = struct.pack("<HHQ", 0x0001, 8, 0)
        insert_offset = local_offset + 30 + local_filename_size + local_extra_size
        bundle[insert_offset:insert_offset] = zip64_extra
        struct.pack_into(
            "<H",
            bundle,
            local_offset + 28,
            local_extra_size + len(zip64_extra),
        )
        footer_offset = bundle.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(footer_offset, 0)
        struct.pack_into("<I", bundle, footer_offset + 16, central_offset + len(zip64_extra))

        with patch(
            "history_service.system_backup.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not run for local ZIP64 extra metadata"),
        ):
            with self.assertRaisesRegex(ValueError, "ZIP64"):
                self.backup_service.import_bundle(bytes(bundle))

    def test_manifest_schema_version_rejects_json_boolean(self) -> None:
        bundle = self._build_zip_bundle(
            {
                "schema_version": True,
                "format": BUNDLE_FORMAT,
                "groups": [],
                "files": [],
            }
        )

        with self.assertRaisesRegex(ValueError, "schema version"):
            self.backup_service.import_bundle(bundle)

    def test_manifest_rejects_duplicate_json_keys_before_materialization(self) -> None:
        manifest_bytes = (
            b'{"schema_version":2,"schema_version":1,"format":"'
            + BUNDLE_FORMAT.encode("ascii")
            + b'","groups":[],"files":[]}'
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", manifest_bytes)

        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self.backup_service.import_bundle(buffer.getvalue())

    def test_manifest_rejects_duplicate_member_reference_before_payload_extraction(self) -> None:
        content = b"payload"
        entry = {
            "key": "duplicate",
            "group_key": SSH_KEYS_KEY,
            "archive_path": "config/ssh/duplicate",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        bundle = self._build_zip_bundle(
            {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "format": BUNDLE_FORMAT,
                "groups": [],
                "files": [entry, dict(entry)],
            },
            {entry["archive_path"]: content},
        )

        with patch.object(
            self.backup_service,
            "_extract_manifest_zip_members",
            side_effect=AssertionError("payload extraction must not start"),
        ):
            with self.assertRaisesRegex(ValueError, "duplicate member key"):
                self.backup_service.import_bundle(bundle)

    def test_manifest_rejects_non_list_group_and_file_collections(self) -> None:
        for field_name in ("groups", "files"):
            with self.subTest(field_name=field_name):
                manifest = {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "format": BUNDLE_FORMAT,
                    "groups": [],
                    "files": [],
                    field_name: {},
                }
                bundle = self._build_zip_bundle(manifest)

                with self.assertRaisesRegex(ValueError, f"{field_name} must be a list"):
                    self.backup_service.import_bundle(bundle)

    def test_tar_expanded_byte_limit_rejects_before_member_reads(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            manifest_bytes = json.dumps(manifest).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
            ignored = b"x" * 2048
            ignored_info = tarfile.TarInfo("ignored.bin")
            ignored_info.size = len(ignored)
            archive.addfile(ignored_info, io.BytesIO(ignored))

        with patch("history_service.system_backup.MAX_ARCHIVE_EXPANDED_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "expanded data exceeds"):
                self.backup_service.import_bundle(buffer.getvalue())

    def test_tar_member_limit_counts_ignored_members(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            manifest_bytes = json.dumps(manifest).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
            for index in range(3):
                info = tarfile.TarInfo(f"ignored-{index}.txt")
                info.size = 0
                archive.addfile(info, io.BytesIO())

        with patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_COUNT", 3):
            with self.assertRaisesRegex(ValueError, "too many members"):
                self.backup_service.import_bundle(buffer.getvalue())

    def test_tar_rejects_unsupported_pax_metadata_before_logical_member_parsing(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            manifest_bytes = json.dumps(manifest).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            manifest_info.pax_headers = {"comment": "x" * 2048}
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        compressed = gzip.compress(tar_buffer.getvalue())

        with self.assertRaisesRegex(ValueError, "PAX metadata key"):
            self.backup_service.import_bundle(compressed)

    def test_tar_rejects_oversized_pax_path_before_logical_member_parsing(self) -> None:
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            member_info = tarfile.TarInfo(f"config/ssh/{'a' * 2048}")
            member_info.size = 0
            archive.addfile(member_info, io.BytesIO())
        compressed = gzip.compress(tar_buffer.getvalue())

        with patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_BYTES", 256):
            with self.assertRaisesRegex(ValueError, "member exceeds"):
                self.backup_service.import_bundle(compressed)

    def test_tar_member_limit_counts_physical_pax_headers(self) -> None:
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for suffix in ("a", "b"):
                member_info = tarfile.TarInfo(f"config/ssh/{suffix * 120}")
                member_info.size = 0
                archive.addfile(member_info, io.BytesIO())

        with patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_COUNT", 3):
            with self.assertRaisesRegex(ValueError, "too many members"):
                self.backup_service._preflight_tar_archive(tar_buffer.getvalue())

    def test_tar_gzip_export_round_trip_preserves_long_member_path(self) -> None:
        content = b"long-path-content"
        archive_path = f"config/ssh/{'a' * 120}"
        member = BundleMember(
            key="ssh-long-path",
            group_key=SSH_KEYS_KEY,
            archive_path=archive_path,
            source_path=None,
            present=True,
            content=content,
        )
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "packaging": "tar.gz",
            "groups": [],
            "files": [
                {
                    "key": member.key,
                    "group_key": member.group_key,
                    "archive_path": archive_path,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }

        archive_bytes = self.backup_service._build_archive([member], manifest, "tar.gz")
        restored_manifest, extracted, packaging, _ = self.backup_service._read_archive(
            archive_bytes
        )

        self.assertEqual(packaging, "tar.gz")
        self.assertEqual(restored_manifest, manifest)
        self.assertEqual(extracted, {member.key: content})

    def test_tar_gzip_rejects_concatenated_member_before_archive_parse(self) -> None:
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            manifest_bytes = json.dumps(manifest).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        concatenated = buffer.getvalue() + gzip.compress(b"\0" * 1024)

        with self.assertRaisesRegex(ValueError, "concatenated gzip"):
            self.backup_service.import_bundle(concatenated)

    def test_tar_gzip_applies_ratio_cap_during_decompression(self) -> None:
        observed_max_lengths: list[int] = []

        class FakeDecompressor:
            eof = True
            unused_data = b""

            def decompress(self, chunk: bytes, max_length: int) -> bytes:
                observed_max_lengths.append(max_length)
                return b""

            def flush(self, length: int) -> bytes:
                return b""

        archive_bytes = b"x" * 100
        with (
            patch("history_service.system_backup.MAX_ARCHIVE_COMPRESSION_RATIO", 2),
            patch("history_service.system_backup.MAX_ARCHIVE_EXPANDED_BYTES", 10_000),
            patch(
                "history_service.system_backup.zlib.decompressobj",
                return_value=FakeDecompressor(),
            ),
        ):
            self.backup_service._decompress_single_gzip_member(archive_bytes)

        self.assertEqual(observed_max_lengths, [201])

    def test_tar_zstd_rejects_concatenated_frame_before_archive_parse(self) -> None:
        if system_backup_module.zstd is None:
            self.skipTest("zstandard is not installed")
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [],
        }
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            manifest_bytes = json.dumps(manifest).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        compressor = system_backup_module.zstd.ZstdCompressor()
        concatenated = compressor.compress(tar_buffer.getvalue()) + compressor.compress(b"\0" * 1024)

        with self.assertRaisesRegex(ValueError, "concatenated zstd"):
            self.backup_service.import_bundle(concatenated)

    def test_7z_limits_count_and_ratio_from_listing_before_extraction(self) -> None:
        entries = [
            {"Path": "manifest.json", "Size": "100", "Packed Size": "10"},
            {"Path": "ignored.bin", "Size": "100", "Packed Size": "1"},
        ]
        with patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_COUNT", 1):
            with self.assertRaisesRegex(ValueError, "too many members"):
                self.backup_service._validate_7z_listed_entries(entries, archive_size=100)
        with patch("history_service.system_backup.MAX_ARCHIVE_COMPRESSION_RATIO", 1):
            with self.assertRaisesRegex(ValueError, "compression ratio"):
                self.backup_service._validate_7z_listed_entries(entries, archive_size=100)

    def test_7z_listing_enforces_available_per_member_packed_sizes(self) -> None:
        entries = [
            {"Path": "manifest.json", "Size": "100", "Packed Size": "1"},
            {"Path": "padding.bin", "Size": "100", "Packed Size": "199"},
        ]

        with patch("history_service.system_backup.MAX_ARCHIVE_COMPRESSION_RATIO", 10):
            with self.assertRaisesRegex(ValueError, "member compression ratio"):
                self.backup_service._validate_7z_listed_entries(entries, archive_size=1000)

    def test_7z_member_limit_counts_directory_entries(self) -> None:
        output = "\n\n".join(
            [
                "Path = bundle.7z\nType = 7z",
                "Path = config\nFolder = +\nAttributes = D....\nSize = 0",
                "Path = config/ssh\nFolder = +\nAttributes = D....\nSize = 0",
                "Path = data\nFolder = +\nAttributes = D....\nSize = 0",
                "Path = manifest.json\nFolder = -\nSize = 100\nPacked Size = 100",
            ]
        )
        entries = self.backup_service._seven_zip_listed_entries(
            output,
            Path("bundle.7z"),
        )

        with patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_COUNT", 1):
            with self.assertRaisesRegex(ValueError, "too many members"):
                self.backup_service._validate_7z_listed_entries(entries, archive_size=1000)

    def test_7z_listing_does_not_hide_member_matching_archive_basename(self) -> None:
        output = "\n\n".join(
            [
                "Path = /tmp/bundle.7z\nType = 7z",
                "Path = bundle.7z\nFolder = -\nSize = 100\nPacked Size = 100",
            ]
        )

        entries = self.backup_service._seven_zip_listed_entries(
            output,
            Path("/tmp/bundle.7z"),
        )

        self.assertEqual([entry["Path"] for entry in entries], ["bundle.7z"])

    def test_7z_member_limit_counts_implicit_extraction_directories(self) -> None:
        entries = [
            {
                "Path": "one/two/manifest.json",
                "Folder": "-",
                "Size": "100",
                "Packed Size": "100",
            }
        ]

        with patch("history_service.system_backup.MAX_ARCHIVE_MEMBER_COUNT", 2):
            with self.assertRaisesRegex(ValueError, "too many members"):
                self.backup_service._validate_7z_listed_entries(entries, archive_size=1000)

    def test_7z_listing_rejects_symbolic_links_before_extraction(self) -> None:
        entries = [
            {
                "Path": "config/ssh/escape",
                "Size": "0",
                "Packed Size": "0",
                "Symbolic Link": "../../outside",
            }
        ]

        with self.assertRaisesRegex(ValueError, "link"):
            self.backup_service._validate_7z_listed_entries(entries, archive_size=100)

    def test_7z_command_output_is_killed_when_runtime_cap_is_crossed(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(b"12345")
                self.stderr = io.BytesIO()
                self.returncode: int | None = None
                self.killed = False

            def wait(self, timeout: int | None = None) -> int:
                self.returncode = -9 if self.killed else 0
                return self.returncode

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

        process = FakeProcess()

        with (
            patch("history_service.system_backup.MAX_7Z_COMMAND_OUTPUT_BYTES", 4),
            patch("history_service.system_backup.subprocess.Popen", return_value=process),
        ):
            with self.assertRaisesRegex(ValueError, "command output exceeded"):
                self.backup_service._run_7z_command(["l", "bundle.7z"])
        self.assertTrue(process.killed)

    def test_7z_timeout_allows_restore_grade_full_history_archives(self) -> None:
        self.assertGreaterEqual(SEVEN_ZIP_TIMEOUT_SECONDS, 600)

    def test_7z_export_uses_memory_bounded_compression_profile(self) -> None:
        archive_path = self.temp_dir / "bounded.7z"
        success = subprocess.CompletedProcess(
            args=["7z"],
            returncode=0,
            stdout="Everything is Ok",
            stderr="",
        )

        with patch.object(self.backup_service, "_run_7z_command", return_value=success) as run_7z:
            self.backup_service._build_7z_archive_to_path(
                [],
                b"{}",
                archive_path,
            )

        command = run_7z.call_args.args[0]
        self.assertIn("-mx=5", command)
        self.assertIn("-mmt=1", command)
        self.assertNotIn("-mx=9", command)

    def test_7z_prompt_channel_keeps_passphrase_out_of_process_argv(self) -> None:
        fake_7z = self.temp_dir / "fake-7z-prompt.py"
        fake_7z.write_text(
            f"""#!{sys.executable}
import os
import sys
import termios

if not os.isatty(0):
    raise SystemExit(3)
attributes = termios.tcgetattr(0)
attributes[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, attributes)
sys.stdout.write("Enter password (will not be echoed):")
sys.stdout.flush()
if not sys.stdin.readline().endswith("\\n"):
    raise SystemExit(4)
sys.stdout.write("\\nEverything is Ok\\n")
sys.stdout.flush()
""",
            encoding="utf-8",
        )
        fake_7z.chmod(0o700)
        passphrase = "synthetic prompt secret   "

        with patch("history_service.system_backup.SEVEN_ZIP_BINARY", str(fake_7z)):
            result = self.backup_service._run_7z_command(
                ["l", "bundle.7z"],
                passphrase=passphrase,
            )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn(passphrase, result.args)
        self.assertNotIn(passphrase, result.stdout)
        self.assertNotIn(passphrase, result.stderr)
        self.assertIn("Everything is Ok", result.stdout)

    def test_7z_prompt_channel_rejects_line_breaks_before_process_start(self) -> None:
        with patch("history_service.system_backup.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "line breaks"):
                self.backup_service._run_7z_command(
                    ["l", "bundle.7z"],
                    passphrase="synthetic\nsecret",
                )

        popen.assert_not_called()

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

    def test_import_preserves_live_targets_for_selected_absent_groups(self) -> None:
        def tree_bytes(root: Path) -> dict[str, bytes]:
            return {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

        original_mapping = self.mapping_path.read_bytes()
        original_ssh = tree_bytes(self.ssh_dir)
        original_history_counts = self.store.counts()
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "packaging": "zip",
            "groups": [
                {
                    "key": MAPPING_FILE_KEY,
                    "selected": True,
                    "present": False,
                    "restore_mode": "file",
                },
                {
                    "key": SSH_KEYS_KEY,
                    "selected": True,
                    "present": False,
                    "restore_mode": "directory",
                },
                {
                    "key": HISTORY_DB_KEY,
                    "selected": True,
                    "present": False,
                    "restore_mode": "history_db",
                },
            ],
            "files": [],
        }

        result = self.backup_service.import_bundle(self._build_zip_bundle(manifest))

        self.assertEqual(self.mapping_path.read_bytes(), original_mapping)
        self.assertEqual(tree_bytes(self.ssh_dir), original_ssh)
        self.assertEqual(self.store.counts(), original_history_counts)
        self.assertEqual(result["restored_paths"], [])
        self.assertEqual(
            result["preserved_absent_groups"],
            [MAPPING_FILE_KEY, HISTORY_DB_KEY, SSH_KEYS_KEY],
        )

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

    def test_restore_limits_cover_production_sized_history_database(self) -> None:
        production_history_bytes = 3_240_521_728

        self.assertGreaterEqual(
            MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
            production_history_bytes,
        )
        self.assertGreaterEqual(
            MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
            production_history_bytes + 1024 * 1024 * 1024,
        )

    def test_file_backed_limit_is_only_available_to_history_manifest_member(self) -> None:
        member = {
            "key": "oversized",
            "group_key": CONFIG_FILE_KEY,
            "archive_path": "config/config.yaml",
            "size_bytes": 3_240_521_728,
            "sha256": "a" * 64,
        }
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "format": BUNDLE_FORMAT,
            "groups": [],
            "files": [member],
        }

        with self.assertRaisesRegex(ValueError, "expanded byte limit"):
            self.backup_service._validate_manifest_before_extraction(
                manifest,
                member_limit=MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                expanded_limit=MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
                large_member_group_keys=frozenset({HISTORY_DB_KEY}),
            )

        history_metadata = BACKUP_GROUP_METADATA[HISTORY_DB_KEY]
        member.update(
            {
                "key": HISTORY_DB_KEY,
                "group_key": HISTORY_DB_KEY,
                "archive_path": str(history_metadata["archive_root"]),
            }
        )
        manifest["groups"] = [
            {
                "key": HISTORY_DB_KEY,
                "archive_root": history_metadata["archive_root"],
                "selected": True,
                "present": True,
                "restore_mode": history_metadata["restore_mode"],
            }
        ]
        self.backup_service._validate_manifest_before_extraction(
            manifest,
            member_limit=MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
            expanded_limit=MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
            large_member_group_keys=frozenset({HISTORY_DB_KEY}),
        )

    def _assert_7z_oversized_manifest_member_rejected_before_payload_extraction(
        self,
        *,
        manifest: dict[str, object],
        archive_path: str,
        expected_error: str,
    ) -> None:
        oversized_size = 3_240_521_728
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        payload_extraction_started = False

        def fake_7z_preflight(
            args: list[str],
            *,
            cwd: Path | None = None,
            passphrase: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            del cwd, passphrase
            nonlocal payload_extraction_started
            if args[0] == "l":
                return subprocess.CompletedProcess(
                    ["7z", *args],
                    0,
                    stdout="\n".join(
                        [
                            f"Path = {Path(args[2]).name}",
                            "Type = 7z",
                            "Method = LZMA2:12",
                            "",
                            "Path = manifest.json",
                            f"Size = {len(manifest_bytes)}",
                            f"Packed Size = {len(manifest_bytes)}",
                            "Encrypted = -",
                            "",
                            f"Path = {archive_path}",
                            f"Size = {oversized_size}",
                            f"Packed Size = {oversized_size}",
                            "Encrypted = -",
                        ]
                    ),
                    stderr="",
                )
            if args[0] == "x":
                selected_members = [
                    argument
                    for argument in args[2:]
                    if not argument.startswith("-")
                ]
                if selected_members != ["manifest.json"]:
                    payload_extraction_started = True
                    raise AssertionError(
                        "7z payload extraction started before manifest policy validation"
                    )
                output_dir = next(
                    Path(argument[2:])
                    for argument in args
                    if argument.startswith("-o")
                )
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "manifest.json").write_bytes(manifest_bytes)
                return subprocess.CompletedProcess(
                    ["7z", *args],
                    0,
                    stdout="Everything is Ok\n",
                    stderr="",
                )
            raise AssertionError(f"Unexpected fake 7z command: {args}")

        with (
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=fake_7z_preflight,
            ),
            patch(
                "history_service.system_backup.MAX_ARCHIVE_COMPRESSION_RATIO",
                oversized_size,
            ),
        ):
            with self.assertRaisesRegex(ValueError, expected_error):
                self.backup_service._read_7z_archive(SEVEN_ZIP_SIGNATURE)

        self.assertFalse(payload_extraction_started)

    def test_7z_rejects_oversized_non_history_member_before_payload_extraction(self) -> None:
        archive_path = "config/config.yaml"
        self._assert_7z_oversized_manifest_member_rejected_before_payload_extraction(
            manifest={
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "format": BUNDLE_FORMAT,
                "packaging": "7z",
                "groups": [],
                "files": [
                    {
                        "key": CONFIG_FILE_KEY,
                        "group_key": CONFIG_FILE_KEY,
                        "archive_path": archive_path,
                        "size_bytes": 1,
                        "sha256": "a" * 64,
                    }
                ],
            },
            archive_path=archive_path,
            expected_error="expanded byte limit",
        )

    def test_7z_rejects_forged_history_group_before_payload_extraction(self) -> None:
        archive_path = "config/not-history.yaml"
        self._assert_7z_oversized_manifest_member_rejected_before_payload_extraction(
            manifest={
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "format": BUNDLE_FORMAT,
                "packaging": "7z",
                "groups": [],
                "files": [
                    {
                        "key": "forged-history",
                        "group_key": HISTORY_DB_KEY,
                        "archive_path": archive_path,
                        "size_bytes": 3_240_521_728,
                        "sha256": "a" * 64,
                    }
                ],
            },
            archive_path=archive_path,
            expected_error="declared backup group",
        )

    def test_7z_rejects_noncanonical_history_member_before_payload_extraction(self) -> None:
        archive_path = "config/not-history.yaml"
        history_metadata = BACKUP_GROUP_METADATA[HISTORY_DB_KEY]
        self._assert_7z_oversized_manifest_member_rejected_before_payload_extraction(
            manifest={
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "format": BUNDLE_FORMAT,
                "packaging": "7z",
                "groups": [
                    {
                        "key": HISTORY_DB_KEY,
                        "archive_root": history_metadata["archive_root"],
                        "selected": True,
                        "present": True,
                        "restore_mode": history_metadata["restore_mode"],
                    }
                ],
                "files": [
                    {
                        "key": "forged-history",
                        "group_key": HISTORY_DB_KEY,
                        "archive_path": archive_path,
                        "size_bytes": 3_240_521_728,
                        "sha256": "a" * 64,
                    }
                ],
            },
            archive_path=archive_path,
            expected_error="declared backup group",
        )

    def test_7z_listing_allows_production_sized_history_member(self) -> None:
        history_size = 3_240_521_728 + 4096
        history_metadata = BACKUP_GROUP_METADATA[HISTORY_DB_KEY]
        manifest = {
            "groups": [
                {
                    "key": HISTORY_DB_KEY,
                    "archive_root": history_metadata["archive_root"],
                    "selected": True,
                    "present": True,
                    "restore_mode": history_metadata["restore_mode"],
                }
            ],
            "files": [
                {
                    "key": HISTORY_DB_KEY,
                    "group_key": HISTORY_DB_KEY,
                    "archive_path": "history/history.sqlite3",
                    "size_bytes": history_size,
                }
            ]
        }
        listed_entries = [
            {"Path": "manifest.json", "Size": "1", "Packed Size": "1"},
            {
                "Path": "history/history.sqlite3",
                "Size": str(history_size),
                "Packed Size": str(history_size),
            },
        ]

        self.backup_service._validate_7z_listing_against_manifest(
            listed_entries,
            manifest,
        )

    def test_7z_listing_keeps_legacy_non_history_expanded_limit(self) -> None:
        member_size = system_backup_module.MAX_ARCHIVE_EXPANDED_BYTES // 2 + 1
        manifest = {
            "files": [
                {
                    "key": "config-a",
                    "group_key": CONFIG_FILE_KEY,
                    "archive_path": "config/a.yaml",
                    "size_bytes": member_size,
                },
                {
                    "key": "config-b",
                    "group_key": CONFIG_FILE_KEY,
                    "archive_path": "config/b.yaml",
                    "size_bytes": member_size,
                },
            ]
        }
        listed_entries = [
            {"Path": "manifest.json", "Size": "1", "Packed Size": "1"},
            {
                "Path": "config/a.yaml",
                "Size": str(member_size),
                "Packed Size": str(member_size),
            },
            {
                "Path": "config/b.yaml",
                "Size": str(member_size),
                "Packed Size": str(member_size),
            },
        ]

        with self.assertRaisesRegex(ValueError, "non-history members"):
            self.backup_service._validate_7z_listing_against_manifest(
                listed_entries,
                manifest,
            )

    def test_file_backed_export_limit_is_only_available_to_history_member(self) -> None:
        member = BundleMember(
            key="oversized",
            group_key=CONFIG_FILE_KEY,
            archive_path="config/config.yaml",
            source_path="synthetic",
            present=True,
            content=b"",
        )
        production_history_bytes = 3_240_521_728

        with patch.object(
            SystemBackupService,
            "_bundle_member_size_and_digest",
            return_value=(production_history_bytes, "a" * 64),
        ):
            with self.assertRaisesRegex(ValueError, "expanded byte limit"):
                self.backup_service._collect_file_specs(
                    [member],
                    member_limit=MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                    expanded_limit=MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
                    large_member_group_keys=frozenset({HISTORY_DB_KEY}),
                )

            member.group_key = HISTORY_DB_KEY
            files = self.backup_service._collect_file_specs(
                [member],
                member_limit=MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                expanded_limit=MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
                large_member_group_keys=frozenset({HISTORY_DB_KEY}),
            )

        self.assertEqual(files[0]["size_bytes"], production_history_bytes)

    def test_history_member_validation_accepts_file_backed_large_database(self) -> None:
        candidate_path = self.temp_dir / "large-history.sqlite3"
        shutil.copyfile(self.history_db_path, candidate_path)
        candidate_path.write_bytes(candidate_path.read_bytes())
        production_history_bytes = 3_240_521_728
        candidate_size = ((production_history_bytes // 4096) + 1) * 4096
        with candidate_path.open("r+b") as candidate:
            candidate.truncate(candidate_size)

        self.assertGreater(candidate_path.stat().st_size, production_history_bytes)

        with patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("large history member must stay file-backed"),
        ):
            SystemBackupService._validate_history_member(candidate_path)

    def test_manifest_metadata_hashes_file_backed_member_without_read_bytes(self) -> None:
        member_path = self.temp_dir / "file-backed-hash.bin"
        member_path.write_bytes(b"file-backed-hash")
        manifest = {
            "files": [
                {
                    "key": "member",
                    "group_key": HISTORY_DB_KEY,
                    "archive_path": "history/history.sqlite3",
                    "size_bytes": member_path.stat().st_size,
                    "sha256": hashlib.sha256(b"file-backed-hash").hexdigest(),
                }
            ]
        }

        with patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("file-backed digest must stream"),
        ):
            self.backup_service._validate_manifest_member_metadata(
                manifest,
                {"member": member_path},
            )

    def test_activation_stages_file_backed_member_without_byte_materialization(self) -> None:
        source_path = self.temp_dir / "file-backed-member.bin"
        source_path.write_bytes(b"file-backed")

        with patch.object(
            _ImportActivationTransaction,
            "_write_staged_bytes",
            side_effect=AssertionError("file-backed member must use file copy"),
        ):
            transaction = _ImportActivationTransaction({"member": source_path})

        try:
            self.assertEqual(transaction._staged_member("member").read_bytes(), b"file-backed")
        finally:
            transaction._cleanup_root()

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
        passphrase: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = args[0]
        argv_passphrase = None
        output_dir: Path | None = None
        archive_path: Path | None = None
        members: list[str] = []

        for raw_arg in args[1:]:
            if raw_arg.startswith("-p"):
                argv_passphrase = raw_arg[2:]
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

        effective_passphrase = passphrase if passphrase is not None else argv_passphrase

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
            archive_path.write_bytes(self._encode_fake_7z_archive(files, effective_passphrase))
            return subprocess.CompletedProcess(
                ["7z", *args],
                0,
                stdout="Everything is Ok\n",
                stderr="",
            )

        payload = self._decode_fake_7z_archive(archive_path)
        expected_passphrase_token = payload.get("passphrase_kdf")
        archive_encrypted = bool(payload.get("encrypted"))
        supplied_passphrase_token = self._fake_7z_passphrase_token(effective_passphrase)
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
                        f"Size = {len(stored_files[relative_path])}",
                        f"Packed Size = {len(stored_files[relative_path])}",
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
            selected_members = set(members)
            for relative_path, content in stored_files.items():
                if selected_members and relative_path not in selected_members:
                    continue
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

    def test_encrypted_7z_import_keeps_history_file_backed_and_cleans_extraction_root(self) -> None:
        passphrase = "file-backed import passphrase"
        validated_paths: list[Path] = []
        real_validate_history = SystemBackupService._validate_history_member

        def record_history_path(content: bytes | Path) -> None:
            self.assertIsInstance(content, Path)
            assert isinstance(content, Path)
            validated_paths.append(content)
            real_validate_history(content)

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=self._fake_7z_command,
            ),
            patch.object(
                self.backup_service,
                "_validate_history_member",
                side_effect=record_history_path,
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_bundle(
                encrypt=True,
                passphrase=passphrase,
                packaging="7z",
                included_paths=[HISTORY_DB_KEY],
            )
            result = self.backup_service.import_bundle(
                artifact.content,
                passphrase=passphrase,
            )

        self.assertTrue(result["restored_history_database"])
        self.assertEqual(len(validated_paths), 1)
        extraction_root = next(
            parent
            for parent in validated_paths[0].parents
            if parent.name.startswith("truenas-jbod-ui-7z-import-")
        )
        self.assertFalse(extraction_root.exists())

    def test_7z_read_failure_fails_closed_when_workspace_cleanup_fails(self) -> None:
        archive = self._encode_fake_7z_archive(
            {"manifest.json": b"{}"},
            "correct passphrase",
        )

        with (
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=self._fake_7z_command,
            ),
            patch(
                "history_service.system_backup.shutil.rmtree",
                side_effect=OSError("synthetic cleanup failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "workspace cleanup failed"):
                self.backup_service.import_bundle(
                    archive,
                    passphrase="wrong passphrase",
                )

    def test_7z_import_workspace_cleanup_failure_precedes_activation(self) -> None:
        passphrase = "cleanup ordering passphrase"
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=self._fake_7z_command,
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_bundle(
                encrypt=True,
                passphrase=passphrase,
                packaging="7z",
                included_paths=[HISTORY_DB_KEY],
            )
            with (
                patch.object(
                    self.backup_service,
                    "_cleanup_extracted_archive",
                    side_effect=RuntimeError("Backup bundle extraction workspace cleanup failed."),
                ),
                patch.object(self.backup_service, "_activate_import_bundle") as activate,
            ):
                with self.assertRaisesRegex(RuntimeError, "workspace cleanup failed"):
                    self.backup_service.import_bundle(
                        artifact.content,
                        passphrase=passphrase,
                    )

        activate.assert_not_called()

    def test_export_without_history_does_not_create_history_snapshot(self) -> None:
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.store,
                "create_backup",
                side_effect=AssertionError("history snapshot must not be created"),
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_bundle(
                packaging="zip",
                included_paths=[CONFIG_FILE_KEY],
            )

        self.assertTrue(artifact.content.startswith(b"PK"))
        self.assertNotIn(HISTORY_DB_KEY, [entry["group_key"] for entry in artifact.manifest["files"]])

    def test_file_export_keeps_history_snapshot_path_backed_during_packaging(self) -> None:
        observed_history_path: Path | None = None

        def build_archive(
            members: list[BundleMember],
            manifest: dict[str, Any],
            packaging: str,
            output_path: Path,
            *,
            passphrase: str | None = None,
        ) -> None:
            nonlocal observed_history_path
            history_member = next(member for member in members if member.group_key == HISTORY_DB_KEY)
            self.assertIsNone(history_member.content)
            self.assertIsNotNone(history_member.file_path)
            assert history_member.file_path is not None
            self.assertTrue(history_member.file_path.is_file())
            observed_history_path = history_member.file_path
            with zipfile.ZipFile(
                output_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                archive.write(history_member.file_path, history_member.archive_path)

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(self.backup_service, "_build_archive_to_path", side_effect=build_archive),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )

        try:
            self.assertTrue(artifact.path.read_bytes().startswith(b"PK"))
            self.assertIsNotNone(observed_history_path)
            assert observed_history_path is not None
            self.assertTrue(observed_history_path.exists())
            history_entry = next(
                entry for entry in artifact.manifest["files"] if entry["group_key"] == HISTORY_DB_KEY
            )
            self.assertEqual(history_entry["size_bytes"], observed_history_path.stat().st_size)
            self.assertEqual(
                history_entry["sha256"],
                hashlib.sha256(observed_history_path.read_bytes()).hexdigest(),
            )
        finally:
            artifact.cleanup()

        self.assertFalse(observed_history_path.exists())

    def test_file_export_round_trips_each_supported_packaging_format(self) -> None:
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with patch.object(self.backup_service, "_run_7z_command", side_effect=self._fake_7z_command):
                for packaging in ("tar.zst", "zip", "tar.gz", "7z"):
                    with self.subTest(packaging=packaging):
                        artifact = self.backup_service.export_bundle_to_file(packaging=packaging)
                        workspace = artifact.cleanup_root
                        try:
                            self.assertTrue(artifact.path.is_file())
                            result = self.backup_service.import_bundle(artifact.path.read_bytes())
                            self.assertTrue(result["ok"])
                            self.assertEqual(result["packaging"], packaging)
                        finally:
                            artifact.cleanup()
                        self.assertFalse(workspace.exists())

    def test_debug_export_without_history_does_not_create_history_snapshot(self) -> None:
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.store,
                "create_backup",
                side_effect=AssertionError("history snapshot must not be created"),
            ),
        ):
            get_settings.cache_clear()
            artifact = self.backup_service.export_debug_bundle_to_file(
                packaging="zip",
                included_paths=[CONFIG_FILE_KEY],
            )

        try:
            self.assertTrue(artifact.path.is_file())
        finally:
            artifact.cleanup()

    def test_file_export_peak_python_memory_is_not_archive_sized(self) -> None:
        large_snapshot = self.temp_dir / "large-history.sqlite3"
        with large_snapshot.open("wb") as output:
            for _ in range(32):
                output.write(os.urandom(1024 * 1024))

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.backup_service,
                "_build_history_snapshot_to_directory",
                return_value=large_snapshot,
            ),
        ):
            get_settings.cache_clear()
            tracemalloc.start()
            artifact = self.backup_service.export_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        try:
            self.assertTrue(artifact.path.is_file())
            self.assertLess(peak_bytes, 8 * 1024 * 1024)
        finally:
            artifact.cleanup()

    def test_file_export_cleans_workspace_when_packaging_fails(self) -> None:
        workspace = self.temp_dir / "failed-file-export"
        workspace.mkdir()

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch("history_service.system_backup.tempfile.mkdtemp", return_value=str(workspace)),
            patch.object(
                self.backup_service,
                "_build_archive_to_path",
                side_effect=ValueError("packaging failed"),
            ),
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "packaging failed"):
                self.backup_service.export_bundle_to_file(
                    packaging="zip",
                    included_paths=[CONFIG_FILE_KEY],
                )

        self.assertFalse(workspace.exists())

    def test_file_export_rejects_oversized_output_and_cleans_workspace(self) -> None:
        workspace = self.temp_dir / "oversized-file-export"
        workspace.mkdir()

        def build_oversized_archive(
            members: list[BundleMember],
            manifest: dict[str, Any],
            packaging: str,
            output_path: Path,
            *,
            passphrase: str | None = None,
        ) -> None:
            with output_path.open("wb") as output:
                output.truncate(MAX_BACKUP_ARCHIVE_BYTES + 1)

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch("history_service.system_backup.tempfile.mkdtemp", return_value=str(workspace)),
            patch.object(
                self.backup_service,
                "_build_archive_to_path",
                side_effect=build_oversized_archive,
            ),
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "archive exceeds"):
                self.backup_service.export_bundle_to_file(
                    packaging="zip",
                    included_paths=[CONFIG_FILE_KEY],
                )

        self.assertFalse(workspace.exists())

    def test_file_export_reserves_member_limit_for_manifest(self) -> None:
        members = [
            BundleMember(
                key=f"member-{index}",
                group_key=CONFIG_FILE_KEY,
                archive_path=f"config/member-{index}",
                source_path=None,
                present=True,
                content=b"",
            )
            for index in range(MAX_ARCHIVE_MEMBER_COUNT)
        ]

        with self.assertRaisesRegex(ValueError, "too many members"):
            self.backup_service._collect_file_specs(members)

    def test_file_export_accepts_restore_grade_history_database_size(self) -> None:
        history_size = 1_200 * 1024 * 1024
        companion_state_size = 64 * 1024 * 1024
        members = [
            BundleMember(
                key="history-db",
                group_key=HISTORY_DB_KEY,
                archive_path="history/history.sqlite3",
                source_path=str(self.temp_dir / "history.sqlite3"),
                present=True,
                content=None,
            ),
            BundleMember(
                key="companion-state",
                group_key=CONFIG_FILE_KEY,
                archive_path="config/state.bin",
                source_path=str(self.temp_dir / "state.bin"),
                present=True,
                content=None,
            ),
        ]

        with patch.object(
            type(self.backup_service),
            "_bundle_member_size_and_digest",
            side_effect=[
                (history_size, "a" * 64),
                (companion_state_size, "b" * 64),
            ],
        ):
            files = self.backup_service._collect_file_specs(members)

        self.assertEqual(
            [item["size_bytes"] for item in files],
            [history_size, companion_state_size],
        )

    def test_tar_file_export_counts_physical_pax_headers(self) -> None:
        members = [
            BundleMember(
                key=f"member-{index}",
                group_key=CONFIG_FILE_KEY,
                archive_path=f"config/{index:04d}-{'x' * 110}",
                source_path=None,
                present=True,
                content=b"",
            )
            for index in range(MAX_ARCHIVE_MEMBER_COUNT - 1)
        ]
        manifest = {"files": self.backup_service._collect_file_specs(members)}
        archive_path = self.temp_dir / "physical-member-boundary.tar.gz"

        with self.assertRaisesRegex(ValueError, "too many members"):
            self.backup_service._build_archive_to_path(
                members,
                manifest,
                "tar.gz",
                archive_path,
            )

    def test_file_export_rejects_incompatible_compression_ratio(self) -> None:
        compressible_snapshot = self.temp_dir / "compressible-history.sqlite3"
        with compressible_snapshot.open("wb") as output:
            output.truncate(4 * 1024 * 1024)

        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False),
            patch.object(
                self.backup_service,
                "_build_history_snapshot_to_directory",
                return_value=compressible_snapshot,
            ),
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "compression ratio"):
                self.backup_service.export_bundle_to_file(
                    packaging="zip",
                    included_paths=[HISTORY_DB_KEY],
                )

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

    def test_encrypted_backup_keeps_passphrase_out_of_every_7z_argument(self) -> None:
        expected_passphrase = "synthetic portable passphrase   "
        observed_commands: list[tuple[list[str], str | None]] = []

        def fake_7z_command(
            args: list[str],
            *,
            cwd: Path | None = None,
            passphrase: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            self.assertFalse(any(expected_passphrase in arg for arg in args))
            observed_commands.append((list(args), passphrase))
            return self._fake_7z_command(
                args,
                cwd=cwd,
                passphrase=passphrase,
            )

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}, clear=False):
            get_settings.cache_clear()
            with patch.object(
                self.backup_service,
                "_run_7z_command",
                side_effect=fake_7z_command,
            ):
                artifact = self.backup_service.export_bundle(
                    encrypt=True,
                    passphrase=expected_passphrase,
                    packaging="7z",
                )
                result = self.backup_service.import_bundle(
                    artifact.content,
                    passphrase=expected_passphrase,
                )

        self.assertEqual(
            [args[0] for args, _ in observed_commands],
            ["a", "l", "l", "x", "x"],
        )
        extract_commands = [args for args, _ in observed_commands if args[0] == "x"]
        self.assertIn("manifest.json", extract_commands[0])
        self.assertNotIn("manifest.json", extract_commands[1])
        self.assertTrue(
            all(prompt_passphrase == expected_passphrase for _, prompt_passphrase in observed_commands)
        )
        self.assertTrue(result["encrypted"])

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

    def test_activation_rejects_file_then_parent_directory_target_collision(self) -> None:
        target_dir = self.temp_dir / "overlapping-target"
        target_dir.mkdir()
        nested_file = target_dir / "mapping.json"
        nested_file.write_bytes(b"ORIGINAL")
        transaction = _ImportActivationTransaction(
            {"file-member": b"IMPORTED-FILE", "directory-member": b"IMPORTED-DIRECTORY"}
        )

        with self.assertRaisesRegex(ValueError, "collides with another restore target"):
            with transaction:
                transaction.activate_file(nested_file, "file-member")
                transaction.activate_directory(
                    target_dir,
                    [("directory-member", Path("key.pem"))],
                )

        self.assertEqual(nested_file.read_bytes(), b"ORIGINAL")
        self.assertEqual(sorted(path.name for path in target_dir.iterdir()), ["mapping.json"])

    def test_activation_rejects_directory_then_nested_file_target_collision(self) -> None:
        target_dir = self.temp_dir / "overlapping-target"
        target_dir.mkdir()
        original_file = target_dir / "original.key"
        original_file.write_bytes(b"ORIGINAL")
        transaction = _ImportActivationTransaction(
            {"directory-member": b"IMPORTED-DIRECTORY", "file-member": b"IMPORTED-FILE"}
        )

        with self.assertRaisesRegex(ValueError, "collides with another restore target"):
            with transaction:
                transaction.activate_directory(
                    target_dir,
                    [("directory-member", Path("key.pem"))],
                )
                transaction.activate_file(target_dir / "mapping.json", "file-member")

        self.assertEqual(original_file.read_bytes(), b"ORIGINAL")
        self.assertEqual(sorted(path.name for path in target_dir.iterdir()), ["original.key"])

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
        with self.assertRaisesRegex(ValueError, "symlink"):
            HistoryStore(str(symlink_path))
        self.assertTrue(symlink_path.is_symlink())

    def test_missing_file_under_symlinked_parent_is_rejected(self) -> None:
        real_parent = self.temp_dir / "real-parent"
        real_parent.mkdir()
        symlinked_parent = self.temp_dir / "symlinked-parent"
        symlinked_parent.symlink_to(real_parent, target_is_directory=True)
        transaction = _ImportActivationTransaction({"member": b"IMPORTED"})

        with self.assertRaisesRegex(ValueError, "symlink"):
            with transaction:
                transaction.activate_file(symlinked_parent / "missing.txt", "member")

        self.assertEqual(list(real_parent.iterdir()), [])

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

    def test_parent_ownership_failure_removes_created_parent_hierarchy(self) -> None:
        hierarchy_root = self.temp_dir / "ownership-failure-parent"
        target_path = hierarchy_root / "nested" / "live.txt"
        transaction = _ImportActivationTransaction({"member": b"IMPORTED"})

        with (
            patch(
                "history_service.system_backup.os.chown",
                side_effect=PermissionError("injected ownership failure"),
            ),
            self.assertRaisesRegex(PermissionError, "injected ownership failure"),
        ):
            with transaction:
                transaction.activate_file(target_path, "member")

        self.assertFalse(hierarchy_root.exists())

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

    def test_activation_reapplies_existing_file_and_directory_ownership(self) -> None:
        file_target = self.temp_dir / "owner-file.txt"
        file_target.write_bytes(b"ORIGINAL")
        directory_target = self.temp_dir / "owner-directory"
        nested_target = directory_target / "nested"
        nested_target.mkdir(parents=True)
        existing_key = nested_target / "id_key"
        existing_key.write_bytes(b"ORIGINAL-KEY")
        expected_owner = (file_target.stat().st_uid, file_target.stat().st_gid)
        transaction = _ImportActivationTransaction(
            {"file": b"IMPORTED", "key": b"IMPORTED-KEY"}
        )

        with patch("history_service.system_backup.os.chown") as chown:
            with transaction:
                transaction.activate_file(file_target, "file")
                transaction.activate_directory(
                    directory_target,
                    [("key", Path("nested/id_key"))],
                )
                transaction.commit()

        self.assertEqual(len(chown.call_args_list), 4)
        self.assertTrue(
            all(
                call.args[1:] == expected_owner
                and call.kwargs == {"follow_symlinks": False}
                for call in chown.call_args_list
            )
        )

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

    def test_activation_inherits_parent_ownership_for_missing_targets(self) -> None:
        file_target = self.temp_dir / "missing-owner-file.txt"
        directory_target = self.temp_dir / "missing-owner-directory"
        expected_owner = (self.temp_dir.stat().st_uid, self.temp_dir.stat().st_gid)
        transaction = _ImportActivationTransaction(
            {"file": b"IMPORTED", "key": b"IMPORTED-KEY"}
        )

        with patch("history_service.system_backup.os.chown") as chown:
            with transaction:
                transaction.activate_file(file_target, "file")
                transaction.activate_directory(
                    directory_target,
                    [("key", Path("nested/id_key"))],
                )
                transaction.commit()

        self.assertEqual(len(chown.call_args_list), 4)
        self.assertTrue(
            all(
                call.args[1:] == expected_owner
                and call.kwargs == {"follow_symlinks": False}
                for call in chown.call_args_list
            )
        )

    def test_activation_inherits_existing_root_owner_for_new_nested_entries(self) -> None:
        directory_target = self.temp_dir / "partial-owner-directory"
        directory_target.mkdir()
        expected_owner = (directory_target.stat().st_uid, directory_target.stat().st_gid)
        transaction = _ImportActivationTransaction({"key": b"IMPORTED-KEY"})

        with patch("history_service.system_backup.os.chown") as chown:
            with transaction:
                transaction.activate_directory(
                    directory_target,
                    [("key", Path("new/id_key"))],
                )
                transaction.commit()

        self.assertEqual(len(chown.call_args_list), 3)
        self.assertTrue(
            all(
                call.args[1:] == expected_owner
                and call.kwargs == {"follow_symlinks": False}
                for call in chown.call_args_list
            )
        )

    def test_activation_inherits_owner_for_created_parent_hierarchy(self) -> None:
        file_target = self.temp_dir / "new" / "nested" / "owner-file.txt"
        expected_owner = (self.temp_dir.stat().st_uid, self.temp_dir.stat().st_gid)
        transaction = _ImportActivationTransaction({"file": b"IMPORTED"})

        with patch("history_service.system_backup.os.chown") as chown:
            with transaction:
                transaction.activate_file(file_target, "file")
                transaction.commit()

        self.assertEqual(len(chown.call_args_list), 3)
        self.assertTrue(
            all(
                call.args[1:] == expected_owner
                and call.kwargs == {"follow_symlinks": False}
                for call in chown.call_args_list
            )
        )

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

    def test_debug_history_scrubbing_includes_metric_rollups(self) -> None:
        connection = sqlite3.connect(self.history_db_path)
        try:
            connection.execute(
                """
                INSERT INTO metric_rollups (
                    bucket_start, bucket_seconds, system_id, system_label,
                    enclosure_key, enclosure_id, enclosure_label, slot, slot_label,
                    metric_name, sample_count, value_sum, value_min, value_max,
                    last_value, last_observed_at,
                    device_name, serial, model, state, gptid,
                    persistent_id_label, disk_identity_key
                ) VALUES (
                    '2026-01-01T10:00:00+00:00', 3600,
                    'archive-core', 'Archive CORE', 'enc-a', 'enc-a',
                    'Front Shelf', 5, '05', 'temperature_c', 2, 60, 29, 31,
                    31, '2026-01-01T10:55:00+00:00',
                    'da5', 'ROLLUP-SERIAL-5', 'Drive', 'healthy', 'gptid/rollup-5',
                    'GPTID', 'serial:ROLLUP-SERIAL-5'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        snapshot_path = self.store.create_backup(self.temp_dir / "scrub-source", retention_count=1)
        self.assertIsNotNone(snapshot_path)
        assert snapshot_path is not None
        target_path = self.temp_dir / "scrubbed-history.sqlite3"

        self.backup_service._build_scrubbed_history_snapshot_file(
            snapshot_path,
            system_backup_module.DebugScrubber(scrub_secrets=False, scrub_disk_identifiers=True),
            target_path,
        )

        scrubbed = sqlite3.connect(target_path)
        try:
            row = scrubbed.execute(
                "SELECT device_name, serial, gptid, disk_identity_key FROM metric_rollups"
            ).fetchone()
        finally:
            scrubbed.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertNotIn("ROLLUP-SERIAL-5", row)
        self.assertNotEqual(row[0], "da5")

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

    def test_system_setup_request_requires_explicit_bmc_host_when_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "BMC host"):
            SystemSetupRequest(
                label="Archive CORE",
                platform="core",
                truenas_host="https://api.example.test",
                bmc_enabled=True,
                bmc_host=None,
                bmc_username="ADMIN",
                bmc_password="secret",
            )

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

    def test_create_system_derives_known_hosts_path_instead_of_persisting_request_value(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(config_path, {})

        service = SystemSetupService(str(config_path))
        service.create_system(
            SystemSetupRequest(
                label="Example CORE",
                platform="core",
                truenas_host="https://core.example.test",
                ssh_enabled=True,
                ssh_user="jbodmap",
                ssh_known_hosts_path=str(temp_dir / "request-selected-known-hosts"),
            )
        )

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["systems"][0]["ssh"]["known_hosts_path"], str(temp_dir / "known_hosts"))

    def test_settings_normalizes_legacy_system_known_hosts_path(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        config_path = temp_dir / "config.yaml"
        write_yaml(
            config_path,
            {
                "systems": [
                    {
                        "id": "legacy-core",
                        "ssh": {"known_hosts_path": str(temp_dir / "legacy-request-selected")},
                    }
                ]
            },
        )

        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(config_path)}, clear=False):
            get_settings.cache_clear()
            settings = get_settings()
            get_settings.cache_clear()

        self.assertEqual(settings.systems[0].ssh.known_hosts_path, str(temp_dir / "known_hosts"))

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
