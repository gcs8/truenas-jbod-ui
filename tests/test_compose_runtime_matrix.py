from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
MATRIX_SCRIPT = ROOT / "scripts" / "run_compose_runtime_matrix.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_CHECKLIST = ROOT / "docs" / "RELEASE_CHECKLIST.md"


class ComposeRuntimeMatrixContractTests(unittest.TestCase):
    def load_matrix_module(self):
        self.assertTrue(MATRIX_SCRIPT.is_file(), "Compose runtime matrix script is missing")
        spec = importlib.util.spec_from_file_location("run_compose_runtime_matrix", MATRIX_SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("Compose runtime matrix script could not be imported")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_matrix_covers_every_supported_sidecar_combination(self) -> None:
        module = self.load_matrix_module()

        variants = {
            variant.name: {
                "profiles": variant.profiles,
                "services": variant.services,
                "ui": variant.ui_enabled,
                "admin_setup": variant.admin_initial_setup,
            }
            for variant in module.VARIANTS
        }

        self.assertEqual(
            variants,
            {
                "ui-only": {
                    "profiles": (),
                    "services": ("enclosure-ui",),
                    "ui": True,
                    "admin_setup": False,
                },
                "ui-history": {
                    "profiles": ("history",),
                    "services": ("enclosure-ui", "enclosure-history"),
                    "ui": True,
                    "admin_setup": False,
                },
                "admin-only": {
                    "profiles": ("admin",),
                    "services": ("enclosure-admin",),
                    "ui": False,
                    "admin_setup": True,
                },
                "ui-admin": {
                    "profiles": ("admin",),
                    "services": ("enclosure-ui", "enclosure-admin"),
                    "ui": True,
                    "admin_setup": False,
                },
                "ui-history-admin": {
                    "profiles": ("history", "admin"),
                    "services": ("enclosure-ui", "enclosure-history", "enclosure-admin"),
                    "ui": True,
                    "admin_setup": False,
                },
            },
        )

    def test_reserved_names_match_base_compose_and_exact_image_revision(self) -> None:
        module = self.load_matrix_module()
        self.assertEqual(
            module.MATRIX_CONTAINER_NAMES,
            {
                "truenas-jbod-ui",
                "truenas-jbod-history",
                "truenas-jbod-admin",
            },
        )
        image_id = "sha256:" + "a" * 64
        source_commit = "b" * 40
        resolved = Mock(returncode=0, stdout=f"{image_id}\n{source_commit}\n")
        with patch.object(module.subprocess, "run", return_value=resolved) as run:
            module.validate_exact_image(image_id, source_commit)
        run.assert_called_once_with(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{.Id}}\n{{index .Config.Labels "org.opencontainers.image.revision"}}',
                image_id,
            ],
            stdout=module.subprocess.PIPE,
            stderr=module.subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
        )
        mismatch = Mock(returncode=0, stdout=f"{image_id}\n{'c' * 40}\n")
        with (
            patch.object(module.subprocess, "run", return_value=mismatch),
            self.assertRaisesRegex(ValueError, "source revision"),
        ):
            module.validate_exact_image(image_id, source_commit)

    def test_matrix_runtime_root_must_be_an_empty_child_of_scratch_root(self) -> None:
        module = self.load_matrix_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            runner_temp = Path(temp_dir)
            valid = runner_temp / "compose-matrix"
            valid.mkdir()
            self.assertEqual(module.validate_runtime_root(valid, runner_temp), valid.resolve())

            with self.assertRaisesRegex(ValueError, "strict child"):
                module.validate_runtime_root(runner_temp, runner_temp)
            with self.assertRaisesRegex(ValueError, "strict child"):
                module.validate_runtime_root(runner_temp.parent / "outside", runner_temp)

            nested_parent = runner_temp / "nested"
            nested_parent.mkdir()
            with self.assertRaisesRegex(ValueError, "direct child"):
                module.validate_runtime_root(nested_parent / "runtime", runner_temp)

            runner_temp.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                module.validate_runtime_root(runner_temp / "permissive", runner_temp)
            runner_temp.chmod(0o700)

            occupied = runner_temp / "occupied"
            occupied.mkdir()
            (occupied / "unexpected").write_text("blocked", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty"):
                module.validate_runtime_root(occupied, runner_temp)

    def test_matrix_source_exercises_pencil_and_admin_initial_setup_writes(self) -> None:
        module = self.load_matrix_module()
        source = MATRIX_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("/api/sas-fabric/aliases", source)
        self.assertIn("sas_fabric_aliases.json", source)
        self.assertIn("/api/admin/system-setup/demo", source)
        self.assertIn("demo-builder-lab", source)
        self.assertIn("anonymous pencil mutation unexpectedly succeeded", source)
        self.assertIn("cross-origin pencil mutation unexpectedly succeeded", source)
        self.assertIn("alias persistence readback failed", source)
        self.assertIn("alias clear readback failed", source)
        self.assertIn("/api/slots/0/mapping", source)
        self.assertIn("slot_mappings.json", source)
        self.assertIn("mapping persistence readback failed", source)
        self.assertIn("mapping clear readback failed", source)
        self.assertIn("admin-only initial setup readback failed", source)
        self.assertIn(
            "compose_runtime_matrix=ok variants=5 ui_alias_cycles=4 ui_mapping_cycles=4 admin_setup_cycles=1",
            source,
        )
        self.assertEqual(sum(variant.ui_enabled for variant in module.VARIANTS), 4)
        self.assertEqual(sum(variant.admin_initial_setup for variant in module.VARIANTS), 1)

    def test_matrix_stays_off_hosted_ci(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("run_compose_runtime_matrix.py", workflow)
        self.assertNotIn("ci_compose_runtime_matrix.py", workflow)

    def test_matrix_requires_explicit_disposable_qa_acknowledgement(self) -> None:
        source = MATRIX_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("I_APPROVE_DISPOSABLE_COMPOSE_QA", source)
        self.assertIn('parser.add_argument("--ack", required=True)', source)
        self.assertIn('parser.add_argument("--source-commit", required=True)', source)
        self.assertNotIn('os.environ.get("CI"', source)

    def test_ui_restart_targets_ui_and_waits_without_dependencies(self) -> None:
        module = self.load_matrix_module()
        prefix = ("docker", "compose", "--project-name", "matrix")
        ports = module.Ports(19080, 19081, 19082)
        with (
            patch.object(module, "_run") as run,
            patch.object(module, "_verify_ui") as verify_ui,
        ):
            module._restart_ui(prefix, ports)
        self.assertEqual(
            run.call_args_list,
            [
                call((*prefix, "restart", "enclosure-ui")),
                call(
                    (
                        *prefix,
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "90",
                        "enclosure-ui",
                    )
                ),
            ],
        )
        verify_ui.assert_called_once_with(ports)

    def test_alias_cycle_restarts_after_persisted_readback_before_clear(self) -> None:
        module = self.load_matrix_module()
        ports = module.Ports(19080, 19081, 19082)
        prefix = ("docker", "compose", "--project-name", "matrix")
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            data.mkdir()
            alias_path = data / "sas_fabric_aliases.json"

            def require_alias(_url, expected, **kwargs):
                self.assertEqual(expected, 200)
                payload = kwargs["payload"]
                if payload["label"] is not None:
                    events.append("save")
                    alias_path.write_text(
                        json.dumps(
                            {
                                "sas_fabric_aliases": {
                                    "synthetic": {
                                        "object_id": "matrix-ui-only",
                                        "label": "Matrix ui-only",
                                    }
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    return b'{"ok":true,"alias":{"label":"Matrix ui-only"}}'
                events.append("clear")
                alias_path.write_text(
                    json.dumps({"sas_fabric_aliases": {}}),
                    encoding="utf-8",
                )
                return b'{"ok":true,"cleared":true}'

            with (
                patch.object(module, "_request", side_effect=[(401, b""), (403, b"")]),
                patch.object(module, "_require_status", side_effect=require_alias),
                patch.object(module, "_restart_ui", side_effect=lambda *_: events.append("restart")),
                patch.object(module, "APP_UID", os.getuid()),
                patch.object(module, "APP_GID", os.getgid()),
            ):
                module._verify_pencil_cycle(root, module.VARIANTS[0], ports, prefix)
        self.assertEqual(events, ["save", "restart", "clear"])

    def test_ui_variants_enable_and_verify_basic_auth(self) -> None:
        module = self.load_matrix_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module._write_environment(
                root,
                "sha256:" + "a" * 64,
                module.Ports(19080, 19081, 19082),
            )
            environment = (root / ".env").read_text(encoding="utf-8")
        self.assertIn("READ_UI_AUTH_MODE=basic", environment)
        self.assertIn("READ_UI_AUTH_USERNAME=operator", environment)
        self.assertIn(
            "READ_UI_AUTH_PASSWORD=synthetic-compose-matrix-passphrase",
            environment,
        )

        with patch.object(module, "_require_status") as require_status:
            module._verify_ui(module.Ports(19080, 19081, 19082))
        self.assertIn(
            call("http://127.0.0.1:19080", 200),
            require_status.call_args_list,
        )
        self.assertNotIn(
            call("http://127.0.0.1:19080", 401),
            require_status.call_args_list,
        )

    def test_ui_backed_history_and_mapping_reads_are_authenticated(self) -> None:
        module = self.load_matrix_module()
        ports = module.Ports(19080, 19081, 19082)
        with patch.object(module, "_require_status") as require_status:
            module._verify_history(ports)
        self.assertIn(
            call(
                "http://127.0.0.1:19080/api/history/status",
                200,
                authenticated=True,
            ),
            require_status.call_args_list,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            data.mkdir()
            mapping_path = data / "slot_mappings.json"
            export_calls: list[dict[str, object]] = []
            clear_urls: list[str] = []
            events: list[str] = []
            prefix = ("docker", "compose", "--project-name", "matrix")

            def require_mapping(url, expected, **kwargs):
                self.assertEqual(expected, 200)
                if url.endswith("/api/mappings/export"):
                    export_calls.append(kwargs)
                    return json.dumps({"revision": "a" * 64}).encode()
                if kwargs.get("method") == "POST":
                    events.append("save")
                    mapping_path.write_text(
                        json.dumps(
                            {
                                "slot_mappings": {
                                    "synthetic": {
                                        "slot": 0,
                                        "notes": "Matrix ui-only",
                                    }
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    return json.dumps(
                        {
                            "ok": True,
                            "mapping": {"notes": "Matrix ui-only"},
                            "snapshot": {
                                "slots": [
                                    {
                                        "slot": 0,
                                        "mapping_clear_revision": "b" * 64,
                                    }
                                ]
                            },
                        }
                    ).encode()
                clear_urls.append(url)
                events.append("clear")
                mapping_path.write_text(
                    json.dumps({"slot_mappings": {}}),
                    encoding="utf-8",
                )
                return b'{"ok":true}'

            with (
                patch.object(module, "_require_status", side_effect=require_mapping),
                patch.object(module, "_restart_ui", side_effect=lambda *_: events.append("restart")),
                patch.object(module, "APP_UID", os.getuid()),
                patch.object(module, "APP_GID", os.getgid()),
            ):
                module._verify_mapping_cycle(root, module.VARIANTS[0], ports, prefix)
        self.assertEqual(
            export_calls,
            [{"authenticated": True}],
        )
        self.assertEqual(len(clear_urls), 1)
        self.assertIn("expected_revision=" + "b" * 64, clear_urls[0])
        self.assertEqual(events, ["save", "restart", "clear"])

    def test_matrix_requires_distinct_unprivileged_free_ports(self) -> None:
        module = self.load_matrix_module()

        self.assertEqual(module.validate_ports(19080, 19081, 19082), (19080, 19081, 19082))
        with self.assertRaisesRegex(ValueError, "distinct"):
            module.validate_ports(19080, 19080, 19082)
        with self.assertRaisesRegex(ValueError, "unprivileged"):
            module.validate_ports(80, 19081, 19082)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            occupied_port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                module.validate_ports_available((occupied_port, 19081, 19082))

    def test_matrix_refuses_insufficient_host_memory(self) -> None:
        module = self.load_matrix_module()

        with self.assertRaisesRegex(RuntimeError, "available memory"):
            module.validate_available_memory(3071 * 1024, minimum_mib=3072)
        self.assertEqual(module.validate_available_memory(3072 * 1024, minimum_mib=3072), 3072)
        self.assertEqual(module.MINIMUM_AVAILABLE_MEMORY_MIB, 3072)

    def test_matrix_refuses_insufficient_scratch_space(self) -> None:
        module = self.load_matrix_module()
        with self.assertRaisesRegex(RuntimeError, "free disk"):
            module.validate_free_disk(4 * 1024**3, minimum_gib=5)
        self.assertEqual(module.validate_free_disk(5 * 1024**3, minimum_gib=5), 5)
        self.assertEqual(module.MINIMUM_FREE_DISK_GIB, 5)

    def test_variant_cleanup_fails_if_compose_or_scratch_cleanup_fails(self) -> None:
        module = self.load_matrix_module()
        prefix = ["docker", "compose", "-p", "synthetic"]
        root = Path("/private/scratch/variant")
        with patch.object(
            module.subprocess,
            "run",
            side_effect=[Mock(returncode=1), Mock(returncode=0)],
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                module._cleanup_variant(prefix, root)
        self.assertEqual(run.call_count, 2)

        with patch.object(
            module.subprocess,
            "run",
            side_effect=[Mock(returncode=0), Mock(returncode=1)],
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                module._cleanup_variant(prefix, root)

    def test_variant_cleanup_readback_requires_reserved_resources_absent(self) -> None:
        module = self.load_matrix_module()
        absent = Mock(returncode=1)
        with patch.object(module.subprocess, "run", return_value=absent) as run:
            module._assert_compose_resources_removed("tjui-matrix-ui-only")
        inspected = [call.args[0] for call in run.call_args_list]
        for name in module.MATRIX_CONTAINER_NAMES:
            self.assertIn(["docker", "container", "inspect", name], inspected)
        self.assertIn(
            ["docker", "network", "inspect", "tjui-matrix-ui-only_default"],
            inspected,
        )
        with (
            patch.object(
                module.subprocess,
                "run",
                return_value=Mock(returncode=0),
            ),
            self.assertRaisesRegex(RuntimeError, "cleanup readback"),
        ):
            module._assert_compose_resources_removed("tjui-matrix-ui-only")

    def test_app_owned_json_readback_uses_bounded_privileged_reader(self) -> None:
        module = self.load_matrix_module()
        completed = Mock(stdout='{"sas_fabric_aliases": {"one": {}}}\n')
        with patch.object(module, "_run", return_value=completed) as run:
            payload = module._read_app_owned_json(Path("/private/state.json"))
        self.assertEqual(payload["sas_fabric_aliases"], {"one": {}})
        run.assert_called_once_with(
            ["sudo", "-n", "--", "cat", "/private/state.json"],
            capture_output=True,
        )

    def test_app_owned_metadata_uses_bounded_privileged_reader(self) -> None:
        module = self.load_matrix_module()
        completed = Mock(stdout="10001:10001:81a0:123\n")
        with patch.object(module, "_run", return_value=completed) as run:
            metadata = module._read_app_owned_metadata(Path("/private/state.json"))
        self.assertEqual(metadata, (10001, 10001, 0o100640, 123))
        run.assert_called_once_with(
            [
                "sudo",
                "-n",
                "--",
                "stat",
                "-c",
                "%u:%g:%f:%s",
                "/private/state.json",
            ],
            capture_output=True,
        )

    def test_release_checklist_keeps_synthetic_and_private_qa_gates_distinct(self) -> None:
        checklist = RELEASE_CHECKLIST.read_text(encoding="utf-8")

        self.assertIn("**Admin only / initial setup:**", checklist)
        self.assertIn("`scripts/run_compose_runtime_matrix.py`", checklist)
        self.assertIn("exact OCI source revision", checklist)
        self.assertIn("save/readback/restart/readback/clear", checklist)
        self.assertIn("production-derived restore is a separate private QA gate", checklist)
        self.assertIn("`--ui-port 19080 --history-port 19081 --admin-port 19082`", checklist)
        self.assertIn("3,072 MiB of available memory", checklist)
        self.assertIn("5 GiB of free scratch space", checklist)


if __name__ == "__main__":
    unittest.main()
