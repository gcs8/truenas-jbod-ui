from __future__ import annotations

import importlib.util
import json
import socket
import stat
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

    def test_inspection_payload_is_exact_and_aggregate_only(self) -> None:
        payload = {
            "ok": True,
            "schema_version": 2,
            "app_version": "0.22.3",
            "exported_at": "2030-01-02T03:04:05+00:00",
            "encrypted": True,
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
            self.assertEqual(stat.S_IMODE((runtime / ".env").stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(override.stat().st_mode),
                0o600,
            )

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
        cleanup = source.index('if not args.keep_running:', stop)
        self.assertIn("service_access.start()", source[start:stop])
        self.assertIn("service_access.close()", source[restart:browser])
        self.assertIn("service_access = _LoopbackProxySet(", source[restart:browser])
        self.assertIn("service_access.start()", source[restart:browser])
        self.assertIn("service_access.close()", source[cleanup:])

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
                )
        self.assertIn(
            ("Origin", "http://127.0.0.1:28082"),
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
                        {"selected_enclosure_id": "synthetic-enclosure"},
                        {"revision": "a" * 64},
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
        self.assertEqual(get_json.call_count, 2)
        self.assertIn("expected_revision=" + "b" * 64, delete_json.call_args.args[1])

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
        with tempfile.TemporaryDirectory() as raw_root:
            raw_dir = Path(raw_root)

            def record_run(command, **kwargs):
                if command[:3] == ["npx", "playwright", "test"]:
                    observed_env.update(kwargs["env"])

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
        config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
        self.assertIn("PLAYWRIGHT_PRIVATE_OUTPUT_DIR", config)
        self.assertIn("PLAYWRIGHT_HTTP_USERNAME_FILE", config)
        self.assertIn("PLAYWRIGHT_HTTP_PASSWORD_FILE", config)

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
        absent = Mock(returncode=1)
        environment = {"PATH": "/safe/bin"}
        with patch.object(self.module.subprocess, "run", return_value=absent) as run:
            self.module._assert_compose_resources_removed(
                "tjuiqa123",
                env=environment,
            )
        inspected = [call.args[0] for call in run.call_args_list]
        for name in self.module.APP_CONTAINER_NAMES:
            self.assertIn(["docker", "container", "inspect", name], inspected)
        self.assertIn(
            ["docker", "network", "inspect", "tjuiqa123_default"],
            inspected,
        )
        self.assertTrue(
            all(call.kwargs.get("env") == environment for call in run.call_args_list)
        )
        with (
            patch.object(
                self.module.subprocess,
                "run",
                return_value=Mock(returncode=0),
            ),
            self.assertRaisesRegex(self.module.QaRestoreError, "cleanup readback"),
        ):
            self.module._assert_compose_resources_removed(
                "tjuiqa123",
                env=environment,
            )

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
            "Request correlation gap",
            "scripts/run_private_qa_restore.py",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
