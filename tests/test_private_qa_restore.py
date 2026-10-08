from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts" / "run_private_qa_restore.py"
DOC = ROOT / "docs" / "PRIVATE_QA_RESTORE.md"
RELEASE_CHECKLIST = ROOT / "docs" / "RELEASE_CHECKLIST.md"
PLAYWRIGHT_CONFIG = ROOT / "playwright.config.js"
BROWSER_SPEC = ROOT / "qa" / "private-restore.spec.js"


class PrivateQaRestoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not CONTROLLER.is_file():
            raise AssertionError("Private QA restore controller is missing")
        spec = importlib.util.spec_from_file_location("run_private_qa_restore", CONTROLLER)
        if spec is None or spec.loader is None:
            raise AssertionError("Unable to load private QA restore controller")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_private_inputs_reject_symlinks_and_permissive_modes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            private_file = root / "private-input"
            private_file.write_bytes(b"synthetic")
            private_file.chmod(0o600)
            self.assertEqual(
                self.module.validate_private_file(private_file, "synthetic input"),
                private_file.resolve(),
            )

            private_file.chmod(0o640)
            with self.assertRaisesRegex(self.module.QaRestoreError, "mode 0600"):
                self.module.validate_private_file(private_file, "synthetic input")

            private_file.chmod(0o600)
            symlink = root / "symlink-input"
            symlink.symlink_to(private_file)
            with self.assertRaisesRegex(self.module.QaRestoreError, "symlink"):
                self.module.validate_private_file(symlink, "synthetic input")

    def test_passphrase_file_preserves_spaces_and_rejects_ambiguous_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "passphrase"
            path.write_text("  synthetic passphrase  \n", encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(
                self.module.read_private_passphrase(path),
                "  synthetic passphrase  ",
            )
            path.write_text("synthetic\n\n", encoding="utf-8")
            with self.assertRaisesRegex(self.module.QaRestoreError, "ambiguous newline"):
                self.module.read_private_passphrase(path)

    def test_release_checklist_requires_encrypted_restore_example(self) -> None:
        checklist = RELEASE_CHECKLIST.read_text(encoding="utf-8")
        self.assertNotIn('`{"encrypt":false,', checklist)
        self.assertIn('`{"encrypt":true,', checklist)

    def test_release_checklist_uses_default_full_format_then_explicit_legacy_7z(self) -> None:
        checklist = RELEASE_CHECKLIST.read_text(encoding="utf-8")
        primary_heading = "**Primary default-format round trip:**"
        legacy_heading = "**Legacy 7z readability round trip:**"
        primary_start = checklist.index(primary_heading)
        legacy_start = checklist.index(legacy_heading)
        legacy_end = checklist.index(
            "  - keep a separate sanitized receipt for each round trip.",
            legacy_start,
        )
        primary = checklist[primary_start:legacy_start]
        legacy = checklist[legacy_start:legacy_end]

        self.assertLess(primary_start, legacy_start)
        self.assertIn("omit `packaging`", primary)
        self.assertIn("observed `tar.zst`", primary)

        from app.models.domain import SystemBackupExportRequest

        bodies = {}
        for round_trip, section in (("primary", primary), ("legacy", legacy)):
            found = re.findall(r"`(\{\"encrypt\".*?\})`", section)
            self.assertEqual(len(found), 1, f"{round_trip} needs exactly one export body")
            body = json.loads(found[0])
            bodies[round_trip] = body
            with self.subTest(round_trip=round_trip):
                self.assertIs(body["encrypt"], True)
                self.assertEqual(body["passphrase"], "<private passphrase, never recorded>")
                self.assertEqual(set(body["included_paths"]), set(self.module.REQUIRED_FULL_GROUPS))
                self.assertEqual(len(body["included_paths"]), len(self.module.REQUIRED_FULL_GROUPS))
                SystemBackupExportRequest.model_validate(body)

        self.assertNotIn("packaging", bodies["primary"])
        self.assertEqual(
            SystemBackupExportRequest.model_validate(bodies["primary"]).packaging, "tar.zst"
        )
        self.assertEqual(bodies["legacy"]["packaging"], "7z")
        for phase in ("export", "inspect", "import", "restart", "readback"):
            with self.subTest(round_trip="primary", phase=phase):
                self.assertIn(phase, primary.lower())

        for phase in ("export", "inspect", "import", "restart", "readback"):
            with self.subTest(round_trip="legacy", phase=phase):
                self.assertIn(phase, legacy.lower())

    def test_restore_receipts_bind_format_export_source_and_candidate_provenance(self) -> None:
        for path in (RELEASE_CHECKLIST, DOC):
            text = path.read_text(encoding="utf-8")
            for marker in (
                "`inspection.packaging`",
                "`inspection.app_version`",
                "`source_commit`",
                "`image_id`",
            ):
                with self.subTest(path=path.name, marker=marker):
                    self.assertIn(marker, text)

    def test_inspection_payload_is_exact_and_aggregate_only(self) -> None:
        payload = {
            "ok": True,
            "schema_version": 2,
            "app_version": "0.22.3",
            "app_version_note": None,
            "exported_at": "2030-01-02T03:04:05+00:00",
            "encrypted": True,
            "encryption_mode": "encrypted",
            "inspection_receipt": "server-issued-single-use-receipt",
            "inspection_receipt_expires_at": 1893553500,
            "packaging": "7z",
            "selected_groups": [
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
            ],
            "present_groups": ["config_file", "history_db"],
            "absent_groups": [
                "runtime_overrides_file",
                "profile_file",
                "mapping_file",
                "sas_fabric_alias_file",
                "slot_detail_file",
                "ssh_keys",
                "tls_trust",
                "known_hosts",
            ],
            "member_count": 3,
            "total_uncompressed_bytes": 4096,
            "aggregate_counts": {
                "systems": 2,
                "profiles": 1,
                "storage_views": 3,
                "mappings": 4,
                "sas_fabric_aliases": 5,
                "slot_details": 6,
                "ssh_keys": 1,
                "tls_files": 2,
                "known_hosts": 1,
                "history": {
                    "tracked_slots": 7,
                    "event_count": 8,
                    "metric_sample_count": 9,
                    "metric_rollup_count": 10,
                },
            },
        }
        validated = self.module.validate_inspection_payload(payload)
        self.assertEqual(validated, payload)

        for unsafe_key in ("systems", "restored_paths", "manifest", "files"):
            with self.subTest(unsafe_key=unsafe_key):
                unsafe = {**payload, unsafe_key: []}
                with self.assertRaisesRegex(self.module.QaRestoreError, "unexpected fields"):
                    self.module.validate_inspection_payload(unsafe)

        older = {
            **payload,
            "app_version_note": "This backup was made by v0.22.2; settings and history will be "
            "brought up to date during restore.",
        }
        self.assertEqual(self.module.validate_inspection_payload(older), older)
        for bad_note in (7, ["note"], "x" * 513):
            with self.subTest(bad_note=bad_note):
                with self.assertRaisesRegex(self.module.QaRestoreError, "app version note"):
                    self.module.validate_inspection_payload({**payload, "app_version_note": bad_note})
        missing_note = {key: value for key, value in payload.items() if key != "app_version_note"}
        with self.assertRaisesRegex(self.module.QaRestoreError, "missing required fields"):
            self.module.validate_inspection_payload(missing_note)

        plaintext = {**payload, "encrypted": False}
        with self.assertRaisesRegex(self.module.QaRestoreError, "encrypted FULL backup"):
            self.module.validate_inspection_payload(plaintext)

        partial = {**payload, "selected_groups": ["config_file", "history_db"]}
        with self.assertRaisesRegex(self.module.QaRestoreError, "FULL backup"):
            self.module.validate_inspection_payload(partial)

        for sensitive_group in ("ssh_keys", "tls_trust", "known_hosts"):
            with self.subTest(sensitive_group=sensitive_group):
                incomplete = {
                    **payload,
                    "selected_groups": [
                        group
                        for group in payload["selected_groups"]
                        if group != sensitive_group
                    ],
                }
                with self.assertRaisesRegex(
                    self.module.QaRestoreError,
                    "FULL backup",
                ):
                    self.module.validate_inspection_payload(incomplete)

    def test_count_reconciliation_compares_every_known_backup_count(self) -> None:
        expected = {
            "systems": 2,
            "profiles": None,
            "history": {
                "tracked_slots": 7,
                "event_count": 8,
            },
        }
        observed = {
            "systems": 2,
            "profiles": 99,
            "history": {
                "tracked_slots": 7,
                "event_count": 8,
            },
        }
        self.module.reconcile_counts(expected, observed)

        observed["history"]["event_count"] = 9
        with self.assertRaisesRegex(
            self.module.QaRestoreError,
            "history.event_count: expected 8, observed 9",
        ):
            self.module.reconcile_counts(expected, observed)

    def test_live_reconciliation_lets_history_grow_but_never_shrink(self) -> None:
        live = self.module.LIVE_GROWABLE_COUNTS
        expected = {"systems": 2, "history": {"tracked_slots": 7, "event_count": 8, "metric_sample_count": 9}}
        grown = {"systems": 2, "history": {"tracked_slots": 8, "event_count": 27, "metric_sample_count": 9}}
        self.module.reconcile_counts(expected, grown, growable=live)
        self.assertEqual(
            self.module.history_growth(expected, grown),
            {"event_count": 19, "metric_sample_count": 0, "tracked_slots": 1},
        )
        # Offline runs stay exact.
        with self.assertRaisesRegex(self.module.QaRestoreError, "history.event_count: expected 8, observed 27"):
            self.module.reconcile_counts(expected, grown)
        for observed, message in (
            ({**grown, "history": {**grown["history"], "event_count": 7}},
             "history.event_count: expected at least 8, observed 7"),
            ({**grown, "history": {**grown["history"], "event_count": True}},
             "history.event_count: expected at least 8, observed True"),
            ({**grown, "systems": 3}, "systems: expected 2, observed 3"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(self.module.QaRestoreError, message):
                self.module.reconcile_counts(expected, observed, growable=live)

    def test_live_checkpoints_fail_when_history_shrinks_between_checks(self) -> None:
        live = self.module.LIVE_GROWABLE_COUNTS
        archive = {"systems": 2, "history": {"event_count": 100, "tracked_slots": 7}}
        startup = {"systems": 2, "history": {"event_count": 119, "tracked_slots": 7}}
        grown = {"systems": 2, "history": {"event_count": 125, "tracked_slots": 8}}
        lost = {"systems": 2, "history": {"event_count": 109, "tracked_slots": 7}}
        self.module.reconcile_checkpoint(archive, startup, None, growable=live)
        self.module.reconcile_checkpoint(archive, grown, startup, growable=live)
        # 109 is still above the archive's 100, but restart lost 10 events.
        self.module.reconcile_counts(archive, lost, growable=live)
        with self.assertRaisesRegex(self.module.QaRestoreError, "history.event_count: expected at least 119, observed 109"):
            self.module.reconcile_checkpoint(archive, lost, startup, growable=live)
        with self.assertRaisesRegex(self.module.QaRestoreError, "history.tracked_slots: expected at least 8, observed 7"):
            self.module.reconcile_checkpoint(archive, {**grown, "history": {**grown["history"], "tracked_slots": 7}},
                                             grown, growable=live)
        # Offline checkpoints stay exact against the archive and each other.
        self.module.reconcile_checkpoint(archive, archive, archive, growable=frozenset())
        with self.assertRaisesRegex(self.module.QaRestoreError, "history.event_count: expected 100, observed 119"):
            self.module.reconcile_checkpoint(archive, startup, archive, growable=frozenset())

    def test_restart_fingerprint_catches_lost_rows_that_new_rows_hide(self) -> None:
        import sqlite3
        import subprocess

        def unprivileged(command, **kwargs):
            self.assertEqual(list(command[:4]), ["sudo", "-n", "--", "python3"])
            return subprocess.run([sys.executable, *command[4:]], capture_output=True, text=True, check=True).stdout

        with tempfile.TemporaryDirectory() as raw_root, \
                patch.object(self.module, "_app_owned_reader", side_effect=unprivileged):
            runtime = Path(raw_root)
            (runtime / "history" / "segments").mkdir(parents=True)
            segment = runtime / "history" / "segments" / "segment-0001.sqlite3"
            segment.write_bytes(b"sealed")
            catalog = runtime / "history" / "segments" / "catalog.json"
            catalog.write_text('{"segments": ["segment-0001"]}')
            database = sqlite3.connect(runtime / "history" / "history.db", isolation_level=None)
            database.executescript("""
                CREATE TABLE slot_state_current (system_id TEXT, enclosure_key TEXT, slot INTEGER, health TEXT,
                    PRIMARY KEY (system_id, enclosure_key, slot));
                CREATE TABLE slot_events (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT);
                CREATE TABLE metric_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT);
                CREATE TABLE metric_rollups (bucket_seconds INTEGER, bucket_start TEXT, metric_name TEXT,
                    PRIMARY KEY (bucket_seconds, bucket_start, metric_name));
                INSERT INTO slot_state_current VALUES ('alpha', 'e', 1, 'OK'), ('alpha', 'e', 2, 'OK');
                INSERT INTO slot_events (observed_at) VALUES ('t1'), ('t2');
                INSERT INTO metric_samples (observed_at) VALUES ('t1'), ('t2'), ('t3');
                ALTER TABLE metric_rollups ADD COLUMN sample_count INTEGER;
                INSERT INTO metric_rollups VALUES (3600, 'h1', 'temp', 12);
            """)
            before = self.module._history_fingerprint(runtime)
            self.assertEqual({table: value["rows"] for table, value in before["tables"].items()}, {
                "slot_state_current": 2, "slot_events": 2, "metric_samples": 3, "metric_rollups": 1})
            self.assertEqual(set(before["segments"]), {"catalog.json", "segment-0001.sqlite3"})
            # Append-only and retention-only tables hash whole rows; upserted slot state hashes its key.
            self.assertEqual(before["tables"]["slot_events"]["columns"], ["id", "observed_at"])
            self.assertEqual(before["tables"]["metric_rollups"]["columns"],
                             ["bucket_seconds", "bucket_start", "metric_name", "sample_count"])
            self.assertEqual(before["tables"]["slot_state_current"]["columns"], ["system_id", "enclosure_key", "slot"])

            def survived():
                self.module.verify_history_survived(before, self.module._history_fingerprint(runtime, before))

            # Live growth, in-place upserts and a column added on restart keep every earlier row.
            database.executescript("""
                INSERT INTO slot_events (observed_at) VALUES ('t3');
                INSERT INTO metric_samples (observed_at) VALUES ('t4'), ('t5');
                INSERT INTO slot_state_current VALUES ('alpha', 'e', 1, 'FAULT')
                    ON CONFLICT (system_id, enclosure_key, slot) DO UPDATE SET health = excluded.health;
                INSERT INTO slot_state_current VALUES ('alpha', 'e', 3, 'OK');
                ALTER TABLE metric_samples ADD COLUMN added_on_restart TEXT DEFAULT 'x';
            """)
            survived()

            for mutate, changed in (
                # A rollback lets a later pass reuse a lost event's ID with new contents.
                (lambda: database.executescript("""
                    DELETE FROM slot_events WHERE id = 2;
                    INSERT INTO slot_events (id, observed_at) VALUES (2, 't2-replayed');
                """), "slot_events"),
                # The counts still grow, but an earlier sample is gone.
                (lambda: database.executescript("""
                    DELETE FROM metric_samples WHERE observed_at = 't2';
                    INSERT INTO metric_samples (observed_at) VALUES ('t6'), ('t7');
                """), "metric_samples, slot_events"),
                # A rollback restores an older aggregate under the same rollup key.
                (lambda: database.execute("UPDATE metric_rollups SET sample_count = 5"),
                 "metric_rollups, metric_samples, slot_events"),
                (lambda: catalog.write_text('{"segments": []}'),
                 "catalog.json, metric_rollups, metric_samples, slot_events"),
                (lambda: segment.write_bytes(b"rewritten"),
                 "catalog.json, metric_rollups, metric_samples, segment-0001.sqlite3, slot_events"),
            ):
                mutate()
                with self.subTest(changed=changed), self.assertRaisesRegex(
                    self.module.QaRestoreError, f"before the restart changed: {changed}$"
                ):
                    survived()
            database.close()

    def test_history_growth_breakdown_shows_status_transitions_and_names_identity_fields(self) -> None:
        import sqlite3
        import subprocess

        with tempfile.TemporaryDirectory() as raw_root:
            runtime = Path(raw_root)
            (runtime / "history").mkdir()
            database = sqlite3.connect(runtime / "history" / "history.db")
            database.execute("CREATE TABLE slot_events (id INTEGER PRIMARY KEY, system_id TEXT, event_type TEXT, details_json TEXT)")
            rows = [
                ("restored", "slot_state_changed", {"health": {"previous": "OK", "current": "FAULT"}}),
                ("alpha", "slot_identity_changed", {"serial": {"previous": "SER-OLD-1", "current": "SER-NEW-1"}}),
                ("alpha", "slot_state_changed", {"health": {"previous": None, "current": "ONLINE"}}),
                ("alpha", "slot_state_changed", {"health": {"previous": None, "current": "ONLINE"}}),
            ]
            database.executemany(
                "INSERT INTO slot_events (system_id, event_type, details_json) VALUES (?, ?, ?)",
                [(system, kind, json.dumps(details)) for system, kind, details in rows],
            )
            database.commit()
            database.close()

            def unprivileged(command):
                self.assertEqual(list(command[:4]), ["sudo", "-n", "--", "python3"])
                return subprocess.run([sys.executable, *command[4:]], capture_output=True, text=True, check=True).stdout

            with patch.object(self.module, "_app_owned_reader", side_effect=unprivileged):
                self.assertEqual(self.module._history_growth_breakdown(runtime, 0), [])
                breakdown = self.module._history_growth_breakdown(runtime, 3)

        self.assertEqual(breakdown, [
            {"system_id": "alpha", "event_type": "slot_state_changed", "changes": ["health: None -> ONLINE"], "count": 2},
            {"system_id": "alpha", "event_type": "slot_identity_changed", "changes": ["serial"], "count": 1},
        ])
        self.assertNotIn("SER-", json.dumps(breakdown))

    def test_import_summary_requires_exact_groups_and_history_activation(self) -> None:
        expected_groups = sorted(self.module.REQUIRED_FULL_GROUPS)
        payload = {
            "ok": True,
            "schema_version": 2,
            "app_version": "0.22.3",
            "encrypted": True,
            "packaging": "7z",
            "included_groups": expected_groups,
            "preserved_absent_groups": [],
            "restored_history_database": True,
            "systems": [],
            "restored_paths": [],
            "stopped_containers": ["ui", "history"],
            "restarted_containers": ["ui", "history"],
            "restart_failures": {},
            "system_count": 0,
        }
        summary = self.module._safe_import_summary(
            payload,
            expected_groups=expected_groups,
        )
        self.assertTrue(summary["restored_history_database"])

        for replacement, message in (
            ({"included_groups": expected_groups[:-1]}, "group set"),
            ({"preserved_absent_groups": ["profile_file"]}, "preserved absent"),
            ({"restored_history_database": False}, "history database"),
            ({"restart_failures": "malformed"}, "restart failures"),
            ({"stopped_containers": []}, "stopped containers"),
            ({"restarted_containers": ["ui"]}, "restarted containers"),
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(self.module.QaRestoreError, message):
                    self.module._safe_import_summary(
                        {**payload, **replacement},
                        expected_groups=expected_groups,
                    )

    def test_history_reconciliation_rejects_missing_or_invalid_counters(self) -> None:
        complete = {key: 0 for key in self.module.HISTORY_COUNT_FIELDS}
        self.assertEqual(self.module._validated_history_counts(complete), complete)
        for replacement in (
            {key: value for key, value in complete.items() if key != "event_count"},
            {**complete, "event_count": True},
            {**complete, "event_count": -1},
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(self.module.QaRestoreError, "history count"):
                    self.module._validated_history_counts(replacement)

    def test_receipts_use_private_modes_and_reject_sensitive_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "evidence"
            receipt = root / "receipt.json"
            payload = {
                "status": "PASS",
                "run_id": "qa-123",
                "source_commit": "a" * 40,
                "image_id": "sha256:" + "b" * 64,
                "backup_sha256": "c" * 64,
                "aggregate_counts_match": True,
            }
            self.module.write_private_json(receipt, payload)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8")), payload)

            with self.assertRaisesRegex(self.module.QaRestoreError, "sensitive receipt key"):
                self.module.write_private_json(root / "unsafe.json", {"systems": []})

            aggregate_receipt = root / "aggregate.json"
            self.module.write_private_json(
                aggregate_receipt,
                {"inspection": {"aggregate_counts": {"systems": 2}}},
            )
            self.assertEqual(
                json.loads(aggregate_receipt.read_text(encoding="utf-8")),
                {"inspection": {"aggregate_counts": {"systems": 2}}},
            )

    def test_fixed_container_name_collision_fails_closed(self) -> None:
        collision = Mock(returncode=0)
        environment = {"PATH": "/safe/bin"}
        with (
            patch.object(self.module.subprocess, "run", return_value=collision) as run,
            self.assertRaisesRegex(self.module.QaRestoreError, "already in use"),
        ):
            self.module._validate_container_names_available(env=environment)
        run.assert_called_once_with(
            ["docker", "container", "inspect", "truenas-jbod-ui"],
            stdout=self.module.subprocess.DEVNULL,
            stderr=self.module.subprocess.DEVNULL,
            timeout=30,
            check=False,
            env=environment,
        )

    def test_exact_image_id_must_exist_locally_and_match(self) -> None:
        image_id = "sha256:" + "a" * 64
        commit = "c" * 40
        environment = {"PATH": "/safe/bin"}
        resolved = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "Id": image_id,
                    "Config": {
                        "Labels": {"org.opencontainers.image.revision": commit}
                    },
                }
            ),
        )
        with patch.object(self.module.subprocess, "run", return_value=resolved) as run:
            self.module._validate_exact_image(image_id, commit, env=environment)
        run.assert_called_once_with(
            ["docker", "image", "inspect", "--format", "{{json .}}", image_id],
            stdout=self.module.subprocess.PIPE,
            stderr=self.module.subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )

        with self.assertRaisesRegex(self.module.QaRestoreError, "sha256 image ID"):
            self.module._validate_exact_image(
                "sha256:" + "z" * 64,
                commit,
                env=environment,
            )
        with (
            patch.object(
                self.module.subprocess,
                "run",
                return_value=Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Id": "sha256:" + "b" * 64,
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": commit
                                }
                            },
                        }
                    ),
                ),
            ),
            self.assertRaisesRegex(self.module.QaRestoreError, "did not resolve"),
        ):
            self.module._validate_exact_image(image_id, commit, env=environment)
        with (
            patch.object(
                self.module.subprocess,
                "run",
                return_value=Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Id": image_id,
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": "d" * 40
                                }
                            },
                        }
                    ),
                ),
            ),
            self.assertRaisesRegex(self.module.QaRestoreError, "source revision"),
        ):
            self.module._validate_exact_image(image_id, commit, env=environment)

    def test_source_commit_must_match_clean_local_checkout(self) -> None:
        commit = "a" * 40
        with patch.object(
            self.module.subprocess,
            "run",
            side_effect=[
                Mock(returncode=0, stdout=commit + "\n"),
                Mock(returncode=0, stdout=""),
            ],
        ) as run:
            self.module._validate_exact_source(ROOT, commit)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                ["git", "-C", str(ROOT), "status", "--porcelain"],
            ],
        )

        with self.assertRaisesRegex(self.module.QaRestoreError, "commit ID"):
            self.module._validate_exact_source(ROOT, "z" * 40)
        with (
            patch.object(
                self.module.subprocess,
                "run",
                side_effect=[
                    Mock(returncode=0, stdout="b" * 40),
                    Mock(returncode=0, stdout=""),
                ],
            ),
            self.assertRaisesRegex(self.module.QaRestoreError, "does not match"),
        ):
            self.module._validate_exact_source(ROOT, commit)

    def test_offline_runtime_override_is_internal_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime = root / "runtime"
            self.module._write_runtime_files(
                ROOT,
                runtime,
                "sha256:" + "a" * 64,
                (28080, 28081, 28082),
                "qa-user",
                "qa-password",
                live_read_only=False,
            )
            override = runtime / "qa-restore.override.yml"
            self.assertEqual(
                self.module.yaml.safe_load(override.read_text(encoding="utf-8")),
                {"networks": {"default": {"internal": True}}},
            )
            environment = (runtime / ".env").read_text(encoding="utf-8")
            self.assertIn("APP_BIND_ADDRESS=127.0.0.1", environment)
            self.assertIn("APP_PUBLIC_ORIGIN=http://127.0.0.1:28080", environment)
            self.assertIn("ADMIN_PUBLIC_ORIGIN=http://127.0.0.1:28082", environment)
            self.assertNotIn("ADMIN_ALLOWED_ORIGINS", environment)
            # Retention must not prune restored history between count checks.
            for kind in ("RAW_METRIC", "EVENT", "HOURLY_ROLLUP", "DAILY_ROLLUP"):
                self.assertIn(f"HISTORY_{kind}_RETENTION_DAYS=0\n", environment)
                self.assertIn(f"${{HISTORY_{kind}_RETENTION_DAYS:-", (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE((runtime / ".env").stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(override.stat().st_mode),
                0o600,
            )

    def test_segmented_history_flag_sets_the_documented_catalog_path(self) -> None:
        for segmented, expected in ((False, False), (True, True)):
            with self.subTest(segmented=segmented), tempfile.TemporaryDirectory() as raw_root:
                runtime = Path(raw_root) / "runtime"
                self.module._write_runtime_files(
                    ROOT,
                    runtime,
                    "sha256:" + "a" * 64,
                    (28080, 28081, 28082),
                    "qa-user",
                    "qa-password",
                    live_read_only=False,
                    segmented_history=segmented,
                )
                environment = (runtime / ".env").read_text(encoding="utf-8")
                self.assertEqual(
                    "HISTORY_SEGMENT_CATALOG_PATH=/app/history/segments/catalog.json" in environment,
                    expected,
                )

    def test_history_mode_must_match_the_backup_before_import(self) -> None:
        self.module.require_matching_history_mode({"schema_version": 2}, segmented_history=True)
        self.module.require_matching_history_mode({"schema_version": 1}, segmented_history=False)
        with self.assertRaisesRegex(self.module.QaRestoreError, "rerun with --segmented-history"):
            self.module.require_matching_history_mode({"schema_version": 2}, segmented_history=False)
        with self.assertRaisesRegex(self.module.QaRestoreError, "single-file history"):
            self.module.require_matching_history_mode({"schema_version": 1}, segmented_history=True)

    def test_drill_schema_constant_matches_the_app(self) -> None:
        from history_service.segment_catalog import SEGMENTED_BACKUP_SCHEMA_VERSION

        self.assertEqual(self.module.SEGMENTED_BACKUP_SCHEMA_VERSION, SEGMENTED_BACKUP_SCHEMA_VERSION)

    def test_loopback_proxy_forwards_and_releases_listener(self) -> None:
        self.assertTrue(hasattr(self.module, "_LoopbackProxySet"))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as target:
            target.bind(("127.0.0.1", 0))
            target.listen(1)
            target_port = target.getsockname()[1]

            def serve_once() -> None:
                connection, _ = target.accept()
                with connection:
                    connection.sendall(b"reply:" + connection.recv(1024))

            thread = threading.Thread(target=serve_once, daemon=True)
            thread.start()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
                reserve.bind(("127.0.0.1", 0))
                proxy_port = reserve.getsockname()[1]

            proxies = self.module._LoopbackProxySet(
                [(proxy_port, "127.0.0.1", target_port)]
            )
            proxies.start()
            try:
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=2) as client:
                    client.sendall(b"synthetic")
                    self.assertEqual(client.recv(1024), b"reply:synthetic")
            finally:
                proxies.close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(self.module._port_is_listening(proxy_port))

    def test_service_access_rejects_nonloopback_or_mismatched_publish(self) -> None:
        self.assertTrue(hasattr(self.module, "_resolve_service_access"))
        environment = {"PATH": "/safe/bin"}

        def metadata(bindings: object, *, address: str = "172.31.0.2") -> str:
            return json.dumps(
                [
                    {
                        "State": {"Status": "running"},
                        "NetworkSettings": {
                            "Networks": {"qa": {"IPAddress": address}},
                            "Ports": {"8000/tcp": bindings},
                        },
                    }
                ]
            )

        with patch.object(
            self.module.subprocess,
            "run",
            return_value=Mock(returncode=0, stdout=metadata(None)),
        ):
            access = self.module._resolve_service_access(
                "truenas-jbod-ui", 8000, 28080, env=environment
            )
        self.assertTrue(access.proxy_required)
        self.assertEqual(access.container_host, "172.31.0.2")

        with patch.object(
            self.module.subprocess,
            "run",
            return_value=Mock(
                returncode=0,
                stdout=metadata(None, address="not-an-ip-address"),
            ),
        ):
            with self.assertRaisesRegex(self.module.QaRestoreError, "IPv4"):
                self.module._resolve_service_access(
                    "truenas-jbod-ui", 8000, 28080, env=environment
                )

        with patch.object(
            self.module.subprocess,
            "run",
            return_value=Mock(
                returncode=0,
                stdout=metadata([{"HostIp": "127.0.0.1", "HostPort": "28080"}]),
            ),
        ):
            direct = self.module._resolve_service_access(
                "truenas-jbod-ui", 8000, 28080, env=environment
            )
        self.assertFalse(direct.proxy_required)

        for bindings in (
            [{"HostIp": "0.0.0.0", "HostPort": "28080"}],
            [{"HostIp": "127.0.0.1", "HostPort": "9999"}],
        ):
            with self.subTest(bindings=bindings), patch.object(
                self.module.subprocess,
                "run",
                return_value=Mock(returncode=0, stdout=metadata(bindings)),
            ):
                with self.assertRaisesRegex(self.module.QaRestoreError, "published binding"):
                    self.module._resolve_service_access(
                        "truenas-jbod-ui", 8000, 28080, env=environment
                    )

    def test_controller_starts_and_closes_loopback_access(self) -> None:
        source = CONTROLLER.read_text(encoding="utf-8")
        start = source.index('phase = "compose-start"')
        stop = source.index('phase = "backup-inspection"', start)
        restart = source.index('phase = "restart-survival"', stop)
        browser = source.index('phase = "browser-and-performance"', restart)
        cleanup = source.index('original_error = sys.exc_info()[1]', stop)
        self.assertIn("service_access.start()", source[start:stop])
        self.assertIn("service_access.close()", source[restart:browser])
        self.assertIn("service_access = _LoopbackProxySet(", source[restart:browser])
        self.assertIn("service_access.start()", source[restart:browser])
        self.assertIn("attempt(service_access.close)", source[cleanup:])

    def test_runtime_preflight_rejects_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            scratch = Path(raw_root)
            scratch.chmod(0o700)
            with (
                patch.object(self.module, "_port_is_free", return_value=True),
                patch.object(
                    self.module,
                    "_available_memory_kib",
                    return_value=8 * 1024**2,
                ),
                patch.object(
                    self.module.shutil,
                    "disk_usage",
                    return_value=Mock(free=20 * 1024**3),
                ),
                self.assertRaisesRegex(self.module.QaRestoreError, "absolute"),
            ):
                self.module._validate_runtime_preflight(
                    scratch,
                    Path("runtime"),
                    (28080, 28081, 28082),
                    3072,
                    10,
                )

    def test_archive_requests_send_the_admin_same_origin_header(self) -> None:
        class FakeResponse:
            status = 200

            def read(self, _limit=None):
                return b'{"ok":true}'

        class FakeConnection:
            instance = None

            def __init__(self, *_args, **_kwargs):
                self.headers = []
                FakeConnection.instance = self

            def putrequest(self, method, path):
                self.method = method
                self.path = path

            def putheader(self, key, value):
                self.headers.append((key, value))

            def endheaders(self):
                return None

            def send(self, _chunk):
                return None

            def getresponse(self):
                return FakeResponse()

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as raw_root:
            archive = Path(raw_root) / "archive.bin"
            archive.write_bytes(b"synthetic")
            with patch.object(
                self.module.http.client,
                "HTTPConnection",
                FakeConnection,
            ):
                self.module.post_archive(
                    28082,
                    "/api/admin/backup/inspect",
                    archive,
                    "passphrase",
                    "username",
                    "password",
                    extra_headers={
                        "X-Backup-Expected-Encryption": "encrypted",
                        "X-Backup-Inspection-Receipt": "server-receipt",
                    },
                )
        self.assertIn(
            ("Origin", "http://127.0.0.1:28082"),
            FakeConnection.instance.headers,
        )
        self.assertIn(
            ("X-Backup-Expected-Encryption", "encrypted"),
            FakeConnection.instance.headers,
        )
        self.assertIn(
            ("X-Backup-Inspection-Receipt", "server-receipt"),
            FakeConnection.instance.headers,
        )

    def test_history_idle_wait_blocks_until_collection_finishes(self) -> None:
        with (
            patch.object(
                self.module,
                "get_json",
                side_effect=[
                    {"collector": {"collection_running": True}},
                    {
                        "collector": {"collection_running": False},
                        "counts": {"event_count": 1},
                    },
                ],
            ) as get_json,
            patch.object(self.module.time, "sleep"),
        ):
            result = self.module._wait_history_idle(
                28081,
                "qa-user",
                "qa-password",
                timeout_seconds=30,
            )
        self.assertFalse(result["collector"]["collection_running"])
        self.assertEqual(get_json.call_count, 2)

    def test_live_pass_wait_needs_an_idle_collector_with_a_completed_pass(self) -> None:
        idle_failed = {"collector": {"collection_running": False, "last_success_at": None}}
        busy_passed = {"collector": {"collection_running": True, "last_success_at": "2026-10-08T04:22:56+00:00"}}
        idle_passed = {"collector": {"collection_running": False, "last_success_at": "2026-10-08T04:22:56+00:00"}}
        with (
            patch.object(self.module, "get_json", side_effect=[idle_failed, busy_passed, idle_passed]) as get_json,
            patch.object(self.module.time, "sleep"),
        ):
            result = self.module._wait_history_idle(28081, "qa-user", "qa-password", timeout_seconds=30, live_pass=True)
        self.assertIs(result, idle_passed)
        self.assertEqual(get_json.call_count, 3)

        # Without live_pass the first idle snapshot is enough, as before.
        with patch.object(self.module, "get_json", return_value=idle_failed), patch.object(self.module.time, "sleep"):
            self.assertIs(self.module._wait_history_idle(28081, "qa-user", "qa-password", timeout_seconds=30), idle_failed)

        # A collector that never finishes a pass fails the gate by name.
        with (
            patch.object(self.module, "get_json", return_value=idle_failed),
            patch.object(self.module.time, "sleep"),
            patch.object(self.module.time, "monotonic", side_effect=[0, 0, 31]),
            self.assertRaisesRegex(self.module.QaRestoreError, "did not finish a live pass after 30s"),
        ):
            self.module._wait_history_idle(28081, "qa-user", "qa-password", timeout_seconds=30, live_pass=True)

    def test_live_browser_checks_start_only_after_a_completed_history_pass(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as raw_root:
            with (
                patch.object(self.module, "_run", side_effect=lambda command, **_: events.append(
                    "live-browser" if "qa/ui-switching.spec.js" in command else "run")),
                patch.object(self.module, "_wait_history_idle", side_effect=lambda *_, **kwargs: events.append(
                    f"history-pass:{kwargs.get('live_pass')}")),
            ):
                results = self.module._run_browser_and_perf(
                    ROOT, (28080, 28081, 28082), "qa-user", "qa-password", Path(raw_root), live_read_only=True,
                )
        self.assertLess(events.index("history-pass:True"), events.index("live-browser"))
        self.assertTrue(results["history_live_pass"])

    def test_observed_state_paths_follow_restored_config_and_stay_in_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runtime = Path(raw_root)
            (runtime / "config").mkdir()
            config = {
                "paths": {
                    "mapping_file": "/app/data/custom/mappings.json",
                    "sas_fabric_alias_file": "/app/data/custom/aliases.json",
                    "slot_detail_cache_file": "/app/data/custom/details.json",
                },
                "ssh": {"known_hosts_path": "/app/data/custom/known_hosts"},
            }
            (runtime / "config" / "config.yaml").write_text(
                yaml.safe_dump(config, sort_keys=False),
                encoding="utf-8",
            )

            paths = self.module._runtime_state_paths(runtime)
            self.assertEqual(paths["mapping_file"], runtime / "data/custom/mappings.json")
            self.assertEqual(paths["sas_fabric_alias_file"], runtime / "data/custom/aliases.json")
            self.assertEqual(paths["slot_detail_cache_file"], runtime / "data/custom/details.json")
            self.assertEqual(paths["known_hosts_path"], runtime / "data/known_hosts")

            config["paths"]["mapping_file"] = "/etc/passwd"
            (runtime / "config" / "config.yaml").write_text(
                yaml.safe_dump(config, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(self.module.QaRestoreError, "outside QA mounts"):
                self.module._runtime_state_paths(runtime)

    def test_app_owned_state_reads_return_json_or_counts_without_printing_content(self) -> None:
        responses = [
            Mock(returncode=0, stdout='{"profiles": [{"id": "private"}]}\n'),
            Mock(returncode=0, stdout="2\n"),
        ]
        with patch.object(self.module.subprocess, "run", side_effect=responses) as run:
            payload = self.module._read_app_owned_json(Path("/private/state.json"))
            count = self.module._count_app_owned_files(Path("/private/keys"))
        self.assertEqual(len(payload["profiles"]), 1)
        self.assertEqual(count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["sudo", "-n", "--", "cat", "/private/state.json"],
        )
        self.assertNotIn("private", run.call_args_list[1].args[0])

    def test_observed_profile_count_excludes_builtin_catalog_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runtime = Path(raw_root)
            for name in ("config/ssh", "config/tls", "data", "history"):
                (runtime / name).mkdir(parents=True, exist_ok=True)
            (runtime / "config" / "config.yaml").write_text(
                "systems:\n  - id: synthetic-system\n",
                encoding="utf-8",
            )
            history_counts = {
                "tracked_slots": 1,
                "event_count": 2,
                "metric_sample_count": 3,
                "metric_rollup_count": 4,
            }
            with patch.object(
                self.module,
                "get_json",
                side_effect=[
                    {
                        "systems": [
                            {"id": "synthetic-system", "storage_views": []}
                        ],
                        "profiles": [
                            {"id": "builtin", "is_custom": False},
                            {"id": "custom", "is_custom": True},
                        ],
                    },
                    {"counts": history_counts},
                ],
            ):
                observed, _system_id = self.module._observed_counts(
                    runtime,
                    (28080, 28081, 28082),
                    "qa-user",
                    "qa-password",
                )
            self.assertEqual(observed["profiles"], 1)

    def test_offline_label_cycle_accepts_real_route_response_shape(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            with patch.object(
                self.module,
                "post_json",
                side_effect=[
                    {
                        "ok": True,
                        "cleared": False,
                        "alias": {"label": "QA restore transient label"},
                    },
                    {"ok": True, "cleared": True, "alias": None},
                ],
            ) as post:
                result = self.module._exercise_pencil_writes(
                    Path(raw_root),
                    28080,
                    "qa-user",
                    "qa-password",
                    "synthetic-system",
                    live_read_only=False,
                )
            self.assertEqual(
                result,
                {"sas_fabric_label": True, "slot_mapping": False},
            )
            self.assertEqual(post.call_count, 2)

    def test_live_mapping_cycle_uses_saved_slot_clear_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            post_count = 0

            def post_response(_port, _path, payload, _username, _password):
                nonlocal post_count
                post_count += 1
                if post_count == 1:
                    return {
                        "ok": True,
                        "cleared": False,
                        "alias": {"label": payload["label"]},
                    }
                if post_count == 2:
                    return {"ok": True, "cleared": True, "alias": None}
                return {
                    "ok": True,
                    "mapping": {"notes": payload["notes"]},
                    "snapshot": {
                        "slots": [
                            {
                                "slot": 0,
                                "mapping_clear_revision": "b" * 64,
                            }
                        ]
                    },
                }

            with (
                patch.object(
                    self.module,
                    "post_json",
                    side_effect=post_response,
                ),
                patch.object(
                    self.module,
                    "get_json",
                    side_effect=[
                        {"selected_enclosure_id": "synthetic-enclosure", "slots": [
                            {"slot": 0, "mapping_revision": "c" * 64},
                        ]},
                        {"revision": "a" * 64, "mappings": []},
                        {"revision": "d" * 64, "mappings": []},
                    ],
                ) as get_json,
                patch.object(
                    self.module,
                    "delete_json",
                    return_value={"ok": True},
                ) as delete_json,
            ):
                result = self.module._exercise_pencil_writes(
                    Path(raw_root),
                    28080,
                    "qa-user",
                    "qa-password",
                    "synthetic-system",
                    live_read_only=True,
                )
        self.assertEqual(
            result,
            {"sas_fabric_label": True, "slot_mapping": True},
        )
        self.assertEqual(get_json.call_count, 3)
        self.assertIn("expected_revision=" + "b" * 64, delete_json.call_args.args[1])

    def test_live_mapping_cycle_real_store_cas_and_preservation(self) -> None:
        from app.models.domain import ManualMapping
        from app.services.mapping_store import MappingRevisionConflict, MappingStore
        import urllib.parse

        for mode in ("empty", "populated", "legacy", "missing-slot", "missing-token", "blank-token", "conflict"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_root:
                path = Path(raw_root) / "mappings.json"
                store = MappingStore(str(path))
                system, enclosure = "synthetic-system", "synthetic-enclosure"
                if mode in {"populated", "legacy"}:
                    store.save_mapping(ManualMapping(
                        system_id=system, enclosure_id=enclosure if mode == "populated" else None,
                        slot=0, notes="original must survive", serial="synthetic-original",
                    ))
                before = path.read_bytes() if path.exists() else None
                scope = store.scope_revision(system, enclosure)
                save = store.save_revision(system, enclosure, 0)
                self.assertNotEqual(scope, save)
                tokens = []

                def get_response(_port, url, *_auth):
                    if "/api/inventory" in url:
                        return {"selected_enclosure_id": enclosure, "slots": [] if mode == "missing-slot" else [{
                            "slot": 0, "enclosure_id": enclosure,
                            "mapping_revision": None if mode == "missing-token" else "" if mode == "blank-token" else save,
                        }]}
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    return {"revision": scope, "mappings": [m.model_dump(mode="json") for m in
                        store.list_mappings(system, query.get("enclosure_id", [None])[0])]}

                def post_response(_port, url, payload, *_auth):
                    if "aliases" in url:
                        return {"ok": True, "cleared": payload["label"] is None,
                                "alias": {"label": payload["label"]} if payload["label"] else None}
                    tokens.append(payload["expected_revision"])
                    if mode == "conflict":
                        store.save_mapping(ManualMapping(system_id=system, enclosure_id=enclosure,
                                                         slot=0, notes="concurrent owner"))
                    saved = store.save_mapping(ManualMapping(system_id=system, enclosure_id=enclosure,
                                                             slot=0, notes=payload["notes"]),
                                               expected_revision=payload["expected_revision"])
                    clear = store.clear_revision(system, enclosure, 0)
                    self.assertNotIn(clear, (scope, save))
                    return {"ok": True, "mapping": saved.model_dump(mode="json"),
                            "snapshot": {"slots": [{"slot": 0, "mapping_clear_revision": clear}]}}

                def delete_response(_port, url, *_auth):
                    token = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["expected_revision"][0]
                    tokens.append(token)
                    return {"ok": store.clear_mapping(system, enclosure, 0, expected_revision=token)}

                with (patch.object(self.module, "get_json", side_effect=get_response),
                      patch.object(self.module, "post_json", side_effect=post_response),
                      patch.object(self.module, "delete_json", side_effect=delete_response)):
                    def invoke():
                        return self.module._exercise_pencil_writes(
                            Path(raw_root), 28080, "qa-user", "qa-password", system, live_read_only=True)
                    if mode == "empty":
                        self.assertTrue(invoke()["slot_mapping"])
                        self.assertEqual(tokens[0], save)
                        self.assertEqual(len(tokens), 2)
                        self.assertIsNone(store.get_mapping(system, enclosure, 0))
                    elif mode == "conflict":
                        with self.assertRaises(MappingRevisionConflict):
                            invoke()
                        self.assertEqual(tokens, [save])
                        self.assertEqual(store.get_mapping(system, enclosure, 0).notes, "concurrent owner")
                    else:
                        with self.assertRaises(self.module.QaRestoreError):
                            invoke()
                        self.assertEqual(tokens, [])
                        self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_live_mapping_guard_scopes_real_store_without_losing_legacy_safety(self) -> None:
        import urllib.parse

        from app.models.domain import ManualMapping
        from app.services.mapping_store import (
            MappingRevisionConflict,
            MappingScopeConflict,
            MappingStore,
        )

        system = "synthetic-system"
        physical = "synthetic-enclosure"
        drawer = physical + "::dell-md1280-drawer-top-42"
        cases = (
            ("other-enclosure", physical, "synthetic-other", system, "success"),
            ("drawer-other-enclosure", drawer, "synthetic-other", system, "success"),
            ("other-enclosure-legacy", physical, "synthetic-other", None, "success"),
            ("unknown-suffix-distinct", physical + "::unknown", physical, system, "success"),
            ("selected-populated", physical, physical, system, "occupied"),
            ("drawer-physical-populated", drawer, physical, system, "occupied"),
            ("physical-drawer-populated", physical, drawer, system, "occupied"),
            ("system-enclosureless", physical, None, system, "occupied"),
            ("global-enclosureless", drawer, None, None, "occupied"),
            ("global-selected", drawer, physical, None, "occupied"),
            ("ambiguous-legacy", drawer, physical, system, "ambiguous"),
            ("clear-conflict", physical, "synthetic-other", system, "conflict"),
        )
        for name, selected, existing_enclosure, existing_system, outcome in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw_root:
                path = Path(raw_root) / "mappings.json"
                store = MappingStore(str(path))
                original = ManualMapping(
                    system_id=existing_system, enclosure_id=existing_enclosure,
                    slot=0, notes="original must survive", serial="SANITIZED-ORIGINAL",
                )
                if outcome == "ambiguous":
                    alias = original.model_copy(update={
                        "system_id": None, "enclosure_id": drawer,
                        "notes": "conflicting legacy owner",
                    })
                    path.write_text(json.dumps({"version": 1, "slot_mappings": {
                        f"{system}:{physical}:0": original.model_dump(mode="json"),
                        f"{drawer}:0": alias.model_dump(mode="json"),
                    }}), encoding="utf-8")
                else:
                    original = store.save_mapping(original)
                before = path.read_bytes()
                requests = []
                mutations = []
                revisions = {}
                conflict_bytes = None

                def request_query(url):
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    self.assertEqual(query["system_id"], [system])
                    return query

                def get_response(_port, url, *_auth):
                    requests.append(url)
                    query = request_query(url)
                    if url.startswith("/api/inventory?"):
                        revisions["save"] = store.save_revision(system, selected, 0)
                        return {"selected_enclosure_id": selected, "slots": [{
                            "slot": 0, "enclosure_id": selected,
                            "mapping_revision": revisions["save"],
                        }]}
                    self.assertTrue(url.startswith("/api/mappings/export?"))
                    scope = query.get("enclosure_id", [None])[0]
                    mappings = store.list_mappings(system, scope)
                    preview = store.preview_replace_mappings(system, scope, mappings)
                    return {"revision": preview["revision"], "mappings": [
                        mapping.model_dump(mode="json") for mapping in mappings
                    ]}

                def post_response(_port, url, payload, *_auth):
                    if url == "/api/sas-fabric/aliases":
                        return {"ok": True, "cleared": payload["label"] is None,
                                "alias": payload if payload["label"] else None}
                    self.assertTrue(url.startswith("/api/slots/0/mapping?"))
                    self.assertEqual(request_query(url)["enclosure_id"], [selected])
                    self.assertEqual(payload["expected_revision"], revisions["save"])
                    self.assertFalse(payload["clear_identify_after_save"])
                    mutations.append("save")
                    saved = store.save_mapping(ManualMapping(
                        system_id=system, enclosure_id=selected, slot=0, notes=payload["notes"],
                    ), expected_revision=payload["expected_revision"])
                    revisions["clear"] = store.clear_revision(system, selected, 0)
                    self.assertNotEqual(revisions["save"], revisions["clear"])
                    return {"ok": True, "mapping": saved.model_dump(mode="json"),
                            "snapshot": {"slots": [{
                                "slot": 0, "mapping_clear_revision": revisions["clear"],
                            }]}}

                def delete_response(_port, url, *_auth):
                    nonlocal conflict_bytes
                    self.assertTrue(url.startswith("/api/slots/0/mapping?"))
                    query = request_query(url)
                    self.assertEqual(query["enclosure_id"], [selected])
                    self.assertEqual(query["expected_revision"], [revisions["clear"]])
                    mutations.append("clear")
                    if outcome == "conflict":
                        store.save_mapping(ManualMapping(
                            system_id=system, enclosure_id=selected, slot=0,
                            notes="concurrent owner must survive",
                        ))
                        conflict_bytes = path.read_bytes()
                    return {"ok": store.clear_mapping(
                        system, selected, 0, expected_revision=query["expected_revision"][0],
                    )}

                with (patch.object(self.module, "get_json", side_effect=get_response),
                      patch.object(self.module, "post_json", side_effect=post_response),
                      patch.object(self.module, "delete_json", side_effect=delete_response)):
                    def invoke():
                        return self.module._exercise_pencil_writes(
                            Path(raw_root), 28080, "qa-user", "qa-password", system,
                            live_read_only=True,
                        )
                    if outcome == "success":
                        self.assertTrue(invoke()["slot_mapping"])
                        self.assertEqual(mutations, ["save", "clear"])
                        reopened = MappingStore(str(path))
                        self.assertIsNone(reopened.get_mapping(system, selected, 0))
                        self.assertEqual(reopened.get_mapping(existing_system, existing_enclosure, 0), original)
                        # A real save/clear updates the document timestamp, but no retained row.
                        self.assertEqual(json.loads(path.read_bytes())["slot_mappings"],
                                         json.loads(before)["slot_mappings"])
                        self.assertEqual(json.loads(path.read_bytes())["version"], json.loads(before)["version"])
                    elif outcome == "conflict":
                        with self.assertRaises(MappingRevisionConflict):
                            invoke()
                        self.assertEqual(mutations, ["save", "clear"])
                        self.assertEqual(path.read_bytes(), conflict_bytes)
                        self.assertEqual(store.get_mapping(system, "synthetic-other", 0), original)
                    else:
                        if outcome == "occupied":
                            with self.assertRaisesRegex(self.module.QaRestoreError, "unpopulated target"):
                                invoke()
                        else:
                            with self.assertRaises(MappingScopeConflict):
                                invoke()
                        self.assertEqual(mutations, [])
                        self.assertEqual(path.read_bytes(), before)
                    if outcome != "ambiguous":
                        self.assertIn("/api/mappings/export?system_id=" + system, requests)

    def test_live_mapping_guard_rejects_invalid_selected_export_before_slot_writes(self) -> None:
        for invalid in (None, {}, [None]):
            with self.subTest(mappings=invalid), tempfile.TemporaryDirectory() as raw_root:
                def post_response(_port, url, payload, *_auth):
                    self.assertEqual(url, "/api/sas-fabric/aliases")
                    return {"ok": True, "cleared": payload["label"] is None,
                            "alias": payload if payload["label"] else None}

                with (patch.object(self.module, "get_json", side_effect=[
                    {"selected_enclosure_id": "synthetic-enclosure", "slots": [
                        {"slot": 0, "mapping_revision": "c" * 64},
                    ]},
                    {"mappings": []},
                    {"mappings": invalid},
                ]), patch.object(self.module, "post_json", side_effect=post_response) as post,
                      patch.object(self.module, "delete_json") as delete):
                    with self.assertRaisesRegex(self.module.QaRestoreError, "mapping export"):
                        self.module._exercise_pencil_writes(
                            Path(raw_root), 28080, "qa-user", "qa-password", "synthetic-system",
                            live_read_only=True,
                        )
                    self.assertEqual(post.call_count, 2)
                    delete.assert_not_called()

    def test_partial_temporary_credential_creation_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            raw_dir = Path(raw_root)
            password_path = raw_dir / ".qa-http-password"
            password_path.write_text("collision", encoding="utf-8")
            password_path.chmod(0o600)
            with self.assertRaises(FileExistsError):
                self.module._run_browser_and_perf(
                    ROOT,
                    (28080, 28081, 28082),
                    "qa-user",
                    "qa-password",
                    raw_dir,
                    live_read_only=False,
                )
            self.assertFalse((raw_dir / ".qa-http-username").exists())
            self.assertEqual(password_path.read_text(encoding="utf-8"), "collision")

    def test_browser_uses_private_file_credentials_and_private_artifact_mode(self) -> None:
        observed_env: dict[str, str] = {}
        observed_umasks: list[object] = []
        with tempfile.TemporaryDirectory() as raw_root:
            raw_dir = Path(raw_root)

            def record_run(command, **kwargs):
                if command[:3] == ["npx", "playwright", "test"]:
                    observed_env.update(kwargs["env"])
                    observed_umasks.append(kwargs.get("umask"))

            with patch.object(self.module, "_run", side_effect=record_run):
                self.module._run_browser_and_perf(
                    ROOT,
                    (28080, 28081, 28082),
                    "private-user-marker",
                    "private-password-marker",
                    raw_dir,
                    live_read_only=False,
                )
        self.assertFalse(
            any(value == "private-user-marker" for value in observed_env.values()),
            "browser environment contained the QA username value",
        )
        self.assertFalse(
            any(value == "private-password-marker" for value in observed_env.values()),
            "browser environment contained the QA password value",
        )
        self.assertIn("PLAYWRIGHT_HTTP_USERNAME_FILE", observed_env)
        self.assertIn("PLAYWRIGHT_HTTP_PASSWORD_FILE", observed_env)
        self.assertIn("PLAYWRIGHT_PRIVATE_OUTPUT_DIR", observed_env)
        # Playwright recreates the output folder, so it must run with a private umask.
        self.assertEqual(observed_umasks, [0o077])
        config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
        self.assertIn("PLAYWRIGHT_PRIVATE_OUTPUT_DIR", config)
        self.assertIn("PLAYWRIGHT_HTTP_USERNAME_FILE", config)
        self.assertIn("PLAYWRIGHT_HTTP_PASSWORD_FILE", config)

    def test_run_applies_the_requested_umask_to_the_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            raw_dir = Path(raw_root)
            target = raw_dir / "made-by-child"
            self.module._run(
                [sys.executable, "-c", f"import os; os.mkdir({str(target)!r})"],
                cwd=raw_dir,
                log_path=raw_dir / "logs" / "child.log",
                timeout=60,
                umask=0o077,
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode) & 0o077, 0)

    def test_private_runtime_root_removal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "runtime"
            root.mkdir()

            def remove_root(*_args, **_kwargs):
                root.rmdir()
                return Mock(returncode=0)

            with patch.object(
                self.module.subprocess,
                "run",
                side_effect=remove_root,
            ) as run:
                self.module._remove_runtime_root(root)
            run.assert_called_once_with(
                ["sudo", "rm", "-rf", str(root)],
                check=False,
            )
            self.assertFalse(root.exists())

            root.mkdir()
            with (
                patch.object(
                    self.module.subprocess,
                    "run",
                    return_value=Mock(returncode=1),
                ),
                self.assertRaisesRegex(self.module.QaRestoreError, "runtime cleanup failed"),
            ):
                self.module._remove_runtime_root(root)

    def test_cleanup_readback_requires_fixed_names_and_project_network_absent(self) -> None:
        environment = {"PATH": "/safe/bin"}
        with patch.object(self.module.subprocess, "run", return_value=Mock(returncode=0, stdout="")) as run:
            self.module._assert_compose_resources_removed("tjuiqa123", env=environment)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[1] for command in commands], ["container", "container", "network", "volume"])
        self.assertTrue(all("label=com.docker.compose.project=tjuiqa123" in c for c in commands[1:]))
        self.assertTrue(all(call.kwargs["env"] == environment for call in run.call_args_list))
        for result in (Mock(returncode=1, stdout=""), Mock(returncode=0, stdout="truenas-jbod-ui running")):
            with patch.object(self.module.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(self.module.QaRestoreError, "cleanup readback"):
                    self.module._assert_compose_resources_removed("tjuiqa123", env=environment)

    def test_main_finalizes_cleanup_after_partial_start_and_failed_teardown(self) -> None:
        import shutil
        from types import SimpleNamespace

        for startup_fails in (True, False):
            for cleanup in ("absent", "down-failed", "down-failed-absent", "residual", "unknown", "volume", "proxy-close"):
                with self.subTest(startup_fails=startup_fails, cleanup=cleanup), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    runtime = root / "runtime"
                    backup, key = root / "backup", root / "key"
                    for path in (backup, key):
                        path.write_text("synthetic")
                        path.chmod(0o600)
                    args = SimpleNamespace(
                        approval=self.module.APPROVAL, live_read_only=False,
                        skip_browser_and_performance=False, target_handle="run-" + "a" * 32,
                        source_commit="b" * 40, image="sha256:" + "c" * 64,
                        backup=backup, passphrase_file=key, app_port=28080, history_port=28081,
                        admin_port=28082, scratch_root=root, runtime_root=runtime,
                        minimum_available_memory_mib=1, minimum_free_disk_gib=1,
                        evidence_dir=root / "evidence", segmented_history=False, keep_running=False,
                    )
                    original = self.module.QaRestoreError("original startup" if startup_fails else "original post-start")
                    events = []

                    def run(command, **kwargs):
                        if "up" in command:
                            events.append("up")
                            if startup_fails:
                                raise original
                        if "down" in command:
                            events.append("down")
                            if cleanup in {"down-failed", "down-failed-absent"}:
                                raise RuntimeError("synthetic down failure")

                    def transport(command, **kwargs):
                        # Real absence/state reader, invented Docker list responses only.
                        events.append("readback")
                        if cleanup == "unknown":
                            return Mock(returncode=1, stdout="", stderr="synthetic unavailable")
                        if "container" in command and cleanup in {"residual", "down-failed"}:
                            return Mock(returncode=0, stdout="truenas-jbod-ui running\n")
                        if "volume" in command and cleanup == "volume":
                            return Mock(returncode=0, stdout="synthetic-volume")
                        return Mock(returncode=0, stdout="")

                    def remove(path):
                        events.append("remove")
                        shutil.rmtree(path)

                    with contextlib.ExitStack() as stack:
                        for name in ("_validate_exact_source", "_validate_exact_image", "_validate_runtime_preflight",
                                     "_validate_container_names_available", "_capture_compose_logs"):
                            stack.enter_context(patch.object(self.module, name))
                        stack.enter_context(patch.object(self.module, "parse_args", return_value=args))
                        stack.enter_context(patch.object(self.module, "_run", side_effect=run))
                        stack.enter_context(patch.object(self.module.subprocess, "run", side_effect=transport))
                        stack.enter_context(patch.object(self.module, "_resolve_service_access", return_value=SimpleNamespace(proxy_required=False)))
                        proxy = Mock()
                        if cleanup == "proxy-close":
                            proxy.close.side_effect = RuntimeError("synthetic proxy close failure")
                        stack.enter_context(patch.object(self.module, "_LoopbackProxySet", return_value=proxy))
                        stack.enter_context(patch.object(self.module, "_wait_json"))
                        stack.enter_context(patch.object(self.module, "post_archive", side_effect=original))
                        stack.enter_context(patch.object(self.module, "_remove_runtime_root", side_effect=remove))
                        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                        with self.assertRaises(self.module.QaRestoreError) as raised:
                            self.module.main()
                    self.assertIs(raised.exception, original)
                    self.assertIn("down", events)
                    self.assertIn("readback", events)
                    receipt = json.loads((args.evidence_dir / "sanitized-receipt.json").read_text())
                    self.assertEqual(receipt["status"], "FAIL")
                    self.assertEqual(receipt["backup_sha256"], self.module.sha256_file(backup))
                    self.assertEqual(receipt["failed_phase"], "compose-start" if startup_fails else "backup-inspection")
                    expected = "verified-stopped" if cleanup in {"absent", "down-failed-absent", "proxy-close"} else "unknown" if cleanup in {"unknown", "volume"} else "running"
                    self.assertEqual(receipt["stack_state"], expected)
                    self.assertIs(receipt["stack_running"], {"verified-stopped": False, "unknown": None, "running": True}[expected])
                    self.assertEqual(runtime.exists(), cleanup not in {"absent", "proxy-close"})
                    if runtime.exists():
                        self.assertTrue((runtime / "docker-compose.yml").is_file())
                    else:
                        self.assertLess(events.index("readback"), events.index("remove"))

    def test_main_success_is_finalized_after_cleanup_or_running_readback(self) -> None:
        import shutil
        from types import SimpleNamespace

        for mode in ("stopped", "live", "keep-running", "down-failed", "readback-unknown"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                backup, key = root / "backup", root / "key"
                for path in (backup, key):
                    path.write_text("synthetic")
                    path.chmod(0o600)
                args = SimpleNamespace(
                    approval=self.module.APPROVAL, live_read_only=mode == "live",
                    live_approval=self.module.LIVE_APPROVAL, skip_browser_and_performance=False,
                    target_handle="run-" + "a" * 32, source_commit="b" * 40, image="sha256:" + "c" * 64,
                    backup=backup, passphrase_file=key, app_port=28080, history_port=28081, admin_port=28082,
                    scratch_root=root, runtime_root=root / "runtime", evidence_dir=root / "evidence",
                    minimum_available_memory_mib=1, minimum_free_disk_gib=1, segmented_history=False,
                    keep_running=mode == "keep-running",
                )
                inspection = {"schema_version": 1, "app_version": "synthetic", "encrypted": True,
                              "encryption_mode": "encrypted", "inspection_receipt": "synthetic-receipt",
                              "packaging": "7z", "selected_groups": [], "present_groups": [], "absent_groups": [],
                              "member_count": 0, "total_uncompressed_bytes": 0,
                              "aggregate_counts": {"history": {"event_count": 3}}}
                breakdown = [{"system_id": "alpha", "event_type": "slot_state_changed", "changes": ["health"], "count": 2}]
                events = []
                def run(command, **kwargs):
                    if "restart" in command:
                        events.append("restart")
                    if "down" in command:
                        events.append("down")
                        if mode == "down-failed":
                            raise RuntimeError("synthetic down failed")
                fingerprints = [{"tables": {"slot_events": {"mark": 3}}, "segments": {}}, {"rehashed": True}]
                def fingerprint(runtime_root, before=None):
                    events.append("fingerprint-after" if before else "fingerprint-before")
                    self.assertIs(before, fingerprints[0] if before else None)
                    return fingerprints[bool(before)]
                def transport(command, **kwargs):
                    events.append("readback")
                    return Mock(returncode=1 if mode == "readback-unknown" else 0,
                                stdout="truenas-jbod-ui running" if "container" in command and mode in {"keep-running", "down-failed"} else "")
                write = self.module.write_private_json
                def receipt_writer(path, receipt):
                    events.append("receipt" if path.name == "sanitized-receipt.json" else path.name)
                    write(path, receipt)
                with contextlib.ExitStack() as stack:
                    for name in ("_validate_exact_source", "_validate_exact_image", "_validate_runtime_preflight",
                                 "_validate_container_names_available", "_capture_compose_logs", "_wait_json",
                                 "_wait_history_idle", "post_archive"):
                        stack.enter_context(patch.object(self.module, name))
                    reconcile = stack.enter_context(patch.object(self.module, "reconcile_checkpoint"))
                    stack.enter_context(patch.object(self.module, "_history_growth_breakdown", return_value=breakdown))
                    stack.enter_context(patch.object(self.module, "_history_fingerprint", side_effect=fingerprint))
                    survived = stack.enter_context(patch.object(self.module, "verify_history_survived"))
                    stack.enter_context(patch.object(self.module, "parse_args", return_value=args))
                    stack.enter_context(patch.object(self.module, "_run", side_effect=run))
                    stack.enter_context(patch.object(self.module.subprocess, "run", side_effect=transport))
                    stack.enter_context(patch.object(self.module, "_resolve_service_access", return_value=SimpleNamespace(proxy_required=False)))
                    stack.enter_context(patch.object(self.module, "_LoopbackProxySet", return_value=Mock()))
                    stack.enter_context(patch.object(self.module, "validate_inspection_payload", return_value=inspection))
                    stack.enter_context(patch.object(self.module, "_safe_import_summary", return_value={}))
                    stack.enter_context(patch.object(self.module, "_observed_counts", return_value=(
                        {"history": {"event_count": 5}}, "synthetic-system")))
                    stack.enter_context(patch.object(self.module, "_exercise_pencil_writes", return_value={"sas_fabric_label": True}))
                    stack.enter_context(patch.object(self.module, "_run_browser_and_perf", return_value={"offline_browser": True}))
                    stack.enter_context(patch.object(self.module, "_remove_runtime_root", side_effect=shutil.rmtree))
                    stack.enter_context(patch.object(self.module, "write_private_json", side_effect=receipt_writer))
                    output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    if mode in {"down-failed", "readback-unknown"}:
                        with self.assertRaises(RuntimeError):
                            self.module.main()
                        self.assertNotIn("private_qa_restore=PASS", output.getvalue())
                    else:
                        self.assertEqual(self.module.main(), 0)
                receipt = json.loads((args.evidence_dir / "sanitized-receipt.json").read_text())
                stopped = mode in {"stopped", "live"}
                self.assertEqual(receipt["status"], "PASS" if stopped or mode == "keep-running" else "FAIL")
                self.assertEqual(receipt["stack_state"], "verified-stopped" if stopped else "unknown" if mode == "readback-unknown" else "running")
                self.assertEqual(events[-1], "receipt")
                self.assertLess(events.index("readback"), events.index("receipt"))
                self.assertEqual("down" in events, mode != "keep-running")
                self.assertEqual(args.runtime_root.exists(), not stopped)
                # Live runs let history grow; the growth is recorded either way.
                growable = self.module.LIVE_GROWABLE_COUNTS if mode == "live" else frozenset()
                self.assertEqual([call.kwargs["growable"] for call in reconcile.call_args_list], [growable] * 3)
                # Each check after the first is chained to the one before it.
                checkpoints = [call.args for call in reconcile.call_args_list]
                self.assertEqual([args[2] is None for args in checkpoints], [True, False, False])
                self.assertIs(checkpoints[1][2], checkpoints[0][1])
                self.assertIs(checkpoints[2][2], checkpoints[1][1])
                # The pre-restart rows are fingerprinted, then rechecked after it.
                self.assertLess(events.index("fingerprint-before"), events.index("restart"))
                self.assertLess(events.index("restart"), events.index("fingerprint-after"))
                survived.assert_called_once_with(*fingerprints)
                self.assertLess(events.index("history-growth.json"), events.index("receipt"))
                self.assertEqual(
                    json.loads((args.evidence_dir / "raw-private" / "history-growth.json").read_text()),
                    {"growth": {"event_count": 2}, "newest_events": breakdown},
                )
                if receipt["status"] == "PASS":
                    self.assertEqual(receipt["history_growth"], {"event_count": 2})

    def test_mandatory_gates_and_opaque_target_handle_fail_closed(self) -> None:
        self.assertEqual(
            self.module._validate_target_handle("run-0123456789abcdef0123456789abcdef"),
            "run-0123456789abcdef0123456789abcdef",
        )
        for value in (
            "/private/path",
            "System Name",
            "10.0.0.1",
            "nas-prod-01",
            "qa-target-01",
            "run-0123456789ABCDEF0123456789abcdef",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(self.module.QaRestoreError, "target handle"):
                    self.module._validate_target_handle(value)
        with self.assertRaisesRegex(self.module.QaRestoreError, "mandatory"):
            self.module._validate_mandatory_gates(skip_browser_and_performance=True)

    def test_compose_child_environment_pins_image_and_loopback(self) -> None:
        image = "sha256:" + "a" * 64
        hostile = {
            "PATH": "/safe/bin",
            "APP_BIND_ADDRESS": "0.0.0.0",
            "JBOD_UI_IMAGE": "sha256:" + "b" * 64,
            "ADMIN_AUTH_PASSWORD": "must-not-survive",
        }
        with patch.dict(self.module.os.environ, hostile, clear=True):
            environment = self.module._compose_child_environment(image)
        self.assertEqual(
            environment,
            {
                "PATH": "/safe/bin",
                "APP_BIND_ADDRESS": "127.0.0.1",
                "JBOD_UI_IMAGE": image,
            },
        )

    def test_controller_source_enforces_full_offline_restore_sequence(self) -> None:
        source = CONTROLLER.read_text(encoding="utf-8")
        for marker in (
            "I_APPROVE_PRIVATE_QA_RESTORE",
            '"internal": True',
            "/api/admin/backup/inspect",
            "/api/admin/backup/import?stop_services=true&restart_services=true",
            "X-Backup-Expected-Encryption",
            "X-Backup-Inspection-Receipt",
            "/api/history/overview?exact_counts=true",
            "/api/sas-fabric/aliases",
            "/api/mappings",
            "docker compose restart",
            "admin-operations.spec.js",
            "run_perf_harness.py",
            "--username-file",
            "--password-file",
            "run_history_perf_harness.py",
            "docker compose logs --no-color",
            "raw-private",
            "sanitized-receipt.json",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        self.assertNotIn(".github/workflows", source)
        self.assertIn("compose_env = _compose_child_environment(args.image)", source)
        self.assertGreaterEqual(source.count("env=compose_env"), 7)
        self.assertLess(
            source.index('phase = "cleanup"'),
            source.index("write_private_json(receipt_path, receipt)"),
        )
        self.assertLess(
            source.index("_remove_runtime_root(args.runtime_root)"),
            source.index("write_private_json(receipt_path, receipt)"),
        )

    def test_private_browser_gate_uses_basic_auth_and_real_label_mutation_route(self) -> None:
        config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
        spec = BROWSER_SPEC.read_text(encoding="utf-8")
        controller = CONTROLLER.read_text(encoding="utf-8")
        for marker in (
            "PLAYWRIGHT_HTTP_USERNAME_FILE",
            "PLAYWRIGHT_HTTP_PASSWORD_FILE",
            "PLAYWRIGHT_PRIVATE_OUTPUT_DIR",
            "httpCredentials",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, config)
        for marker in (
            "/api/sas-fabric/aliases",
            "QA restore browser label",
            'label: null',
            "pageerror",
            "console",
            "#system-select",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, spec)
        self.assertIn("saved.alias.label", spec)
        self.assertNotIn("saved.label", spec)
        self.assertIn('"PLAYWRIGHT_PRIVATE_OUTPUT_DIR"', controller)

    def test_documentation_maps_variants_touchpoints_and_privacy_boundary(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for marker in (
            "UI only",
            "UI + history",
            "Admin only / initial setup",
            "UI + admin",
            "UI + history + admin",
            "One-shot FULL backup",
            "SAS Fabric labels",
            "slot mappings",
            "production-derived",
            "egress-blocked",
            "loopback proxy",
            "raw-private",
            "sanitized-receipt.json",
            "run-<32 lowercase hex>",
            "ssh_keys",
            "tls_trust",
            "known_hosts",
            "single-use inspection receipt",
            "observed encryption mode",
            "Request correlation gap",
            "scripts/run_private_qa_restore.py",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_offline_runtime_override_follows_the_offline_network_constant(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime = root / "runtime"
            with patch.object(
                self.module,
                "OFFLINE_NETWORK",
                {"internal": True, "attachable": False},
            ):
                self.module._write_runtime_files(
                    ROOT,
                    runtime,
                    "sha256:" + "a" * 64,
                    (28080, 28081, 28082),
                    "qa-user",
                    "qa-password",
                    live_read_only=False,
                )
            override = runtime / "qa-restore.override.yml"
            self.assertEqual(
                yaml.safe_load(override.read_text(encoding="utf-8")),
                {"networks": {"default": {"internal": True, "attachable": False}}},
            )

    def test_cleanup_failure_raises_when_it_is_the_first_failure(self) -> None:
        def failing_cleanup() -> None:
            raise RuntimeError("synthetic cleanup failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
            self.module._cleanup_after_run(failing_cleanup, "synthetic cleanup")

    def test_cleanup_failure_is_reported_and_suppressed_while_another_error_propagates(self) -> None:
        class BodyFailure(RuntimeError):
            pass

        def failing_cleanup() -> None:
            raise RuntimeError("synthetic cleanup failure")

        stderr = io.StringIO()
        with self.assertRaises(BodyFailure), contextlib.redirect_stderr(stderr):
            try:
                raise BodyFailure("original failure")
            finally:
                self.module._cleanup_after_run(failing_cleanup, "synthetic cleanup")

        report = stderr.getvalue()
        self.assertIn("synthetic cleanup", report)
        self.assertIn("synthetic cleanup failure", report)


if __name__ == "__main__":
    unittest.main()
