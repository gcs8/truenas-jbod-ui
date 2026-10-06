from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.parse
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
                "scheduler": variant.scheduler_enabled,
                "scheduler_policy": variant.scheduler_policy_enabled,
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
                    "scheduler": False,
                    "scheduler_policy": False,
                },
                "ui-history": {
                    "profiles": ("history",),
                    "services": ("enclosure-ui", "enclosure-history"),
                    "ui": True,
                    "admin_setup": False,
                    "scheduler": False,
                    "scheduler_policy": False,
                },
                "admin-only": {
                    "profiles": ("admin",),
                    "services": ("enclosure-admin",),
                    "ui": False,
                    "admin_setup": True,
                    "scheduler": False,
                    "scheduler_policy": False,
                },
                "ui-admin": {
                    "profiles": ("admin",),
                    "services": ("enclosure-ui", "enclosure-admin"),
                    "ui": True,
                    "admin_setup": False,
                    "scheduler": False,
                    "scheduler_policy": False,
                },
                "ui-history-admin": {
                    "profiles": ("history", "admin"),
                    "services": ("enclosure-ui", "enclosure-history", "enclosure-admin"),
                    "ui": True,
                    "admin_setup": False,
                    "scheduler": False,
                    "scheduler_policy": False,
                },
                "scheduler-disabled": {
                    "profiles": ("backup-scheduler",),
                    "services": ("enclosure-backup-scheduler",),
                    "ui": False,
                    "admin_setup": False,
                    "scheduler": True,
                    "scheduler_policy": False,
                },
                "scheduler-enabled": {
                    "profiles": ("admin", "backup-scheduler"),
                    "services": ("enclosure-admin", "enclosure-backup-scheduler"),
                    "ui": False,
                    "admin_setup": False,
                    "scheduler": True,
                    "scheduler_policy": True,
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
            env=module._child_environment(),
        )
        mismatch = Mock(returncode=0, stdout=f"{image_id}\n{'c' * 40}\n")
        with (
            patch.object(module.subprocess, "run", return_value=mismatch),
            self.assertRaisesRegex(ValueError, "source revision"),
        ):
            module.validate_exact_image(image_id, source_commit)

    def test_child_environment_isolates_docker_config_without_hiding_cli_plugins(self) -> None:
        module = self.load_matrix_module()
        environment = module._child_environment()

        self.assertEqual(set(environment), {"PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST"})
        self.assertEqual(environment["DOCKER_HOST"], "unix:///var/run/docker.sock")
        # /dev/null or a path under an unreadable directory makes Ubuntu's
        # docker.io CLI drop the system compose plugin; an empty private
        # directory is an empty config that still finds it.
        config = Path(environment["DOCKER_CONFIG"])
        metadata = config.stat(follow_symlinks=False)
        self.assertTrue(stat.S_ISDIR(metadata.st_mode))
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
        self.assertEqual(list(config.iterdir()), [])
        self.assertEqual(module._child_environment(), environment)

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
        self.assertEqual(
            module.SUCCESS_MARKER,
            "compose_runtime_matrix=ok variants=7 ui_alias_cycles=4 "
            "ui_mapping_cycles=4 admin_setup_cycles=1 scheduler_disabled_cycles=1 "
            "scheduler_backup_cycles=1",
        )
        self.assertEqual(sum(variant.ui_enabled for variant in module.VARIANTS), 4)
        self.assertEqual(sum(variant.admin_initial_setup for variant in module.VARIANTS), 1)
        self.assertEqual(sum(variant.scheduler_enabled for variant in module.VARIANTS), 2)
        self.assertEqual(sum(variant.scheduler_policy_enabled for variant in module.VARIANTS), 1)

    def test_scheduler_variants_write_disabled_and_enabled_policy_environment(self) -> None:
        module = self.load_matrix_module()
        variants = {variant.name: variant for variant in module.VARIANTS}
        image = "sha256:" + "a" * 64
        ports = module.Ports(19080, 19081, 19082)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module._write_environment(
                root,
                image,
                ports,
                variant=variants["scheduler-disabled"],
            )
            disabled = (root / ".env").read_text(encoding="utf-8")
            module._write_environment(
                root,
                image,
                ports,
                variant=variants["scheduler-enabled"],
            )
            enabled = (root / ".env").read_text(encoding="utf-8")

        for environment in (disabled, enabled):
            self.assertIn("BACKUP_TARGETS_JSON=[]\n", environment)
        self.assertIn("BACKUP_CONFIG_ENABLED=false", disabled)
        self.assertIn("BACKUP_FULL_ENABLED=false", disabled)
        self.assertNotIn("BACKUP_ARCHIVE_PASSPHRASE_FILE=", disabled)
        self.assertIn("BACKUP_CONFIG_ENABLED=true", enabled)
        self.assertIn("BACKUP_FULL_ENABLED=false", enabled)
        self.assertIn(
            "BACKUP_ARCHIVE_PASSPHRASE_FILE=/run/backup-secrets/archive-passphrase",
            enabled,
        )

    def test_matrix_environment_replaces_fixture_backup_targets(self) -> None:
        import yaml

        from history_service.backup_archive.policy import load_backup_policy

        module = self.load_matrix_module()
        variants = {variant.name: variant for variant in module.VARIANTS}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module._write_environment(
                root,
                "sha256:" + "a" * 64,
                module.Ports(19080, 19081, 19082),
                variant=variants["scheduler-enabled"],
            )
            environment = dict(
                line.split("=", 1)
                for line in (root / ".env").read_text(encoding="utf-8").splitlines()
                if line
            )
            config = root / "config.yaml"
            config.write_text(
                yaml.safe_dump(
                    {
                        "backups": {
                            "targets": [
                                {"target_id": "remote", "provider": "filesystem", "root": str(root / "remote")}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            policy = load_backup_policy(config, environment)
        self.assertEqual(policy.targets, ())

    def test_disabled_scheduler_serves_socket_health_with_both_classes_off(self) -> None:
        module = self.load_matrix_module()
        prefix = ("docker", "compose", "--project-name", "matrix")
        library = {
            "available": True,
            "running": None,
            "classes": {
                "config": {"enabled": False},
                "full": {"enabled": False},
            },
            "artifacts": [],
        }
        with patch.object(
            module,
            "_scheduler_request",
            side_effect=[
                {"status": 200, "payload": {"status": "ok"}},
                {"status": 200, "payload": library},
            ],
        ) as request:
            module._verify_scheduler_disabled(prefix)

        self.assertEqual(
            request.call_args_list,
            [
                call(prefix, "GET", "/internal/healthz"),
                call(prefix, "GET", "/internal/backups"),
            ],
        )

    def test_enabled_scheduler_runs_verifies_restarts_and_reads_back_one_backup(self) -> None:
        module = self.load_matrix_module()
        prefix = ("docker", "compose", "--project-name", "matrix")
        ports = module.Ports(19080, 19081, 19082)
        initial = {
            "available": True,
            "running": None,
            "classes": {
                "config": {"enabled": True, "last_run": None},
                "full": {"enabled": False},
            },
            "artifacts": [],
        }
        running = {**initial, "running": {"backup_class": "config"}}
        artifact = {
            "id": "synthetic-config-backup",
            "backup_class": "config",
            "location": "local",
            "size": 123,
            "verified": True,
            "restorable": True,
            "state": "ok",
        }
        completed = {
            **initial,
            "classes": {
                "config": {"enabled": True, "last_run": {"ok": True}},
                "full": {"enabled": False},
            },
            "artifacts": [artifact],
        }
        with (
            patch.object(
                module,
                "_scheduler_request",
                side_effect=[
                    {"status": 200, "payload": {"status": "ok"}},
                    {"status": 200, "payload": {"status": "ok"}},
                ],
            ) as scheduler_request,
            patch.object(
                module,
                "_require_status",
                side_effect=[
                    json.dumps(initial).encode(),
                    b'{"ok":true,"backup_class":"config","state":"started"}',
                    json.dumps(running).encode(),
                    json.dumps(completed).encode(),
                    b'{"ok":true}',
                    json.dumps(completed).encode(),
                ],
            ) as require_status,
            patch.object(module, "_run") as run,
            patch.object(module.time, "sleep"),
        ):
            module._verify_scheduler_enabled(ports, prefix)

        self.assertEqual(scheduler_request.call_count, 2)
        self.assertEqual(
            run.call_args_list,
            [
                call((*prefix, "restart", "enclosure-backup-scheduler")),
                call(
                    (
                        *prefix,
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "90",
                        "enclosure-backup-scheduler",
                    )
                ),
            ],
        )
        self.assertEqual(require_status.call_count, 6)
        self.assertEqual(
            require_status.call_args_list[1],
            call(
                "http://127.0.0.1:19082/api/admin/backups/run",
                202,
                method="POST",
                payload={"backup_class": "config"},
                authenticated=True,
                origin="http://127.0.0.1:19082",
            ),
        )
        self.assertEqual(
            require_status.call_args_list[4],
            call(
                "http://127.0.0.1:19082/api/admin/backups/synthetic-config-backup/verify",
                200,
                method="POST",
                authenticated=True,
                origin="http://127.0.0.1:19082",
            ),
        )

    def _scheduler_libraries(self):
        initial = {
            "available": True,
            "running": None,
            "classes": {"config": {"enabled": True, "last_run": None}, "full": {"enabled": False}},
            "artifacts": [],
        }
        artifact = {
            "id": "synthetic-config-backup",
            "backup_class": "config",
            "location": "local",
            "size": 123,
            "verified": True,
            "restorable": True,
            "state": "ok",
        }
        completed = {
            **initial,
            "classes": {"config": {"enabled": True, "last_run": {"ok": True}}, "full": {"enabled": False}},
            "artifacts": [artifact],
        }
        return initial, artifact, completed

    def _run_enabled_with(self, module, verify_body: bytes, restart_library: dict) -> None:
        initial, _, completed = self._scheduler_libraries()
        with (
            patch.object(
                module,
                "_scheduler_request",
                return_value={"status": 200, "payload": {"status": "ok"}},
            ),
            patch.object(
                module,
                "_require_status",
                side_effect=[
                    json.dumps(initial).encode(),
                    b'{"ok":true,"backup_class":"config","state":"started"}',
                    json.dumps(completed).encode(),
                    verify_body,
                    json.dumps(restart_library).encode(),
                ],
            ),
            patch.object(module, "_run"),
            patch.object(module.time, "sleep"),
        ):
            module._verify_scheduler_enabled(
                module.Ports(19080, 19081, 19082), ("docker", "compose", "-p", "m")
            )

    def test_enabled_scheduler_rejects_failed_verify_and_changed_restart_artifact(self) -> None:
        module = self.load_matrix_module()
        _, artifact, completed = self._scheduler_libraries()

        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            self._run_enabled_with(module, b'{"ok":false}', completed)

        replaced = {**completed, "artifacts": [{**artifact, "id": "another-backup"}]}
        with self.assertRaisesRegex(RuntimeError, "restart readback"):
            self._run_enabled_with(module, b'{"ok":true}', replaced)

        self._run_enabled_with(module, b'{"ok":true}', completed)

    def test_disabled_scheduler_rejects_an_artifact_running_job_or_enabled_class(self) -> None:
        module = self.load_matrix_module()
        idle = {
            "running": None,
            "classes": {"config": {"enabled": False}, "full": {"enabled": False}},
            "artifacts": [],
        }
        bad_libraries = (
            {**idle, "artifacts": [{"id": "unexpected"}]},
            {**idle, "running": {"backup_class": "config"}},
            {**idle, "classes": {"config": {"enabled": True}, "full": {"enabled": False}}},
        )
        for library in bad_libraries:
            with self.subTest(library=library):
                with (
                    patch.object(
                        module,
                        "_scheduler_request",
                        side_effect=[
                            {"status": 200, "payload": {"status": "ok"}},
                            {"status": 200, "payload": library},
                        ],
                    ),
                    self.assertRaisesRegex(RuntimeError, "Disabled backup scheduler"),
                ):
                    module._verify_scheduler_disabled(("docker", "compose", "-p", "m"))

    def test_run_variant_dispatches_the_matching_scheduler_check(self) -> None:
        module = self.load_matrix_module()
        variants = {variant.name: variant for variant in module.VARIANTS}
        expected = {
            "scheduler-disabled": ("_verify_scheduler_disabled", "_verify_scheduler_enabled"),
            "scheduler-enabled": ("_verify_scheduler_enabled", "_verify_scheduler_disabled"),
        }
        for name, (called, skipped) in expected.items():
            variant = variants[name]
            running = Mock(stdout="\n".join(variant.services) + "\n")
            with self.subTest(variant=name), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(module, "_prepare_variant_root"))
                stack.enter_context(patch.object(module, "_compose_prefix", return_value=["docker", "compose", "-p", "synthetic"]))
                stack.enter_context(patch.object(module, "_run", return_value=running))
                stack.enter_context(patch.object(module, "_cleanup_variant"))
                stack.enter_context(patch.object(module, "_assert_compose_resources_removed"))
                stack.enter_context(patch.object(module, "_verify_rendered_contract"))
                stack.enter_context(patch.object(module, "_verify_runtime_contract"))
                stack.enter_context(patch.object(module, "_verify_admin"))
                mocks = {
                    helper: stack.enter_context(patch.object(module, helper))
                    for helper in ("_verify_scheduler_disabled", "_verify_scheduler_enabled")
                }
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                module._run_variant(
                    Path("/private/scratch"),
                    variant,
                    compose_path=Path("/private/compose.yaml"),
                    config_fixture=Path("/private/config.yaml"),
                    image="sha256:" + "a" * 64,
                    ports=module.Ports(19080, 19081, 19082),
                    source_commit="b" * 40,
                )
                mocks[called].assert_called_once()
                mocks[skipped].assert_not_called()

    def test_initial_setup_variant_starts_from_a_config_without_systems(self) -> None:
        import yaml

        module = self.load_matrix_module()
        for variant in module.VARIANTS:
            with self.subTest(variant=variant.name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "variant"
                compose = Path(temp_dir) / "compose.yaml"
                compose.write_text("services: {}\n", encoding="utf-8")
                fixture = Path(temp_dir) / "config.yaml"
                with patch.object(module, "_run") as run:
                    module._prepare_variant_root(
                        root,
                        variant=variant,
                        compose_path=compose,
                        config_fixture=fixture,
                        image="sha256:" + "a" * 64,
                        ports=module.Ports(19080, 19081, 19082),
                    )
                commands = [tuple(c.args[0]) for c in run.call_args_list]
                installed = [
                    cmd for cmd in commands if cmd[-1] == str(root / "config" / "config.yaml")
                ]
                self.assertEqual(len(installed), 1)
                source = Path(installed[0][-2])
                if variant.admin_initial_setup:
                    self.assertNotEqual(source, fixture)
                    config = yaml.safe_load(source.read_text(encoding="utf-8"))
                    self.assertNotIn("systems", config)
                    self.assertNotIn("truenas", config)
                else:
                    self.assertEqual(source, fixture)

    def test_scheduler_variant_root_installs_shared_dirs_and_private_passphrase(self) -> None:
        module = self.load_matrix_module()
        variants = {variant.name: variant for variant in module.VARIANTS}
        for name, expect_passphrase in (("scheduler-disabled", False), ("scheduler-enabled", True)):
            with self.subTest(variant=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "variant"
                compose = Path(temp_dir) / "compose.yaml"
                compose.write_text("services: {}\n", encoding="utf-8")
                with patch.object(module, "_run") as run:
                    module._prepare_variant_root(
                        root,
                        variant=variants[name],
                        compose_path=compose,
                        config_fixture=Path(temp_dir) / "config.yaml",
                        image="sha256:" + "a" * 64,
                        ports=module.Ports(19080, 19081, 19082),
                    )
                commands = [tuple(c.args[0]) for c in run.call_args_list]
                shared = next(
                    cmd for cmd in commands if str(root / "backup-journal") in cmd
                )
                self.assertIn("2770", shared)
                self.assertIn(str(root / "backup-api"), shared)
                passphrase = [
                    cmd
                    for cmd in commands
                    if cmd[-1] == str(root / "config" / "backup-secrets" / "archive-passphrase")
                ]
                if expect_passphrase:
                    self.assertEqual(len(passphrase), 1)
                    self.assertIn("0600", passphrase[0])
                    self.assertIn(str(module.BACKUP_UID), passphrase[0])
                else:
                    self.assertEqual(passphrase, [])
                self.assertFalse((root / ".synthetic-backup-passphrase").exists())

    def test_variant_cleanup_removes_volumes_and_orphans(self) -> None:
        module = self.load_matrix_module()
        prefix = ["docker", "compose", "-p", "synthetic"]
        with (
            patch.object(module.subprocess, "run", return_value=Mock(returncode=0)) as run,
            patch.object(module, "_assert_compose_resources_removed"),
        ):
            module._cleanup_variant(prefix, Path("/private/scratch/missing-variant"))
        self.assertEqual(
            run.call_args_list[0].args[0],
            (*prefix, "down", "--volumes", "--remove-orphans"),
        )

    def test_release_docs_list_both_scheduler_variants(self) -> None:
        checklist = " ".join(RELEASE_CHECKLIST.read_text(encoding="utf-8").split())
        private_qa = (ROOT / "docs" / "PRIVATE_QA_RESTORE.md").read_text(encoding="utf-8")
        contributing = " ".join((ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").split())
        for text in (
            "**Scheduler disabled:** start only `enclosure-backup-scheduler`",
            "**Scheduler enabled:** start `enclosure-admin` plus `enclosure-backup-scheduler`",
            "restart the scheduler, confirm the same artifact from the persisted catalogue",
        ):
            self.assertIn(text, checklist)
        self.assertIn("| Scheduler disabled | Scheduler only;", private_qa)
        self.assertIn("| Scheduler enabled | Scheduler + admin;", private_qa)
        self.assertIn("5. Scheduler disabled:", contributing)
        self.assertIn("6. Scheduler enabled:", contributing)
        self.assertIn("restart/readback retains the same catalogued artifact", contributing)

    def test_architecture_guide_scopes_the_scheduler_to_source_checkouts(self) -> None:
        guide = " ".join(
            (ROOT / "wiki" / "Architecture-and-Services.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("**Backup scheduler: current-source checkout only.**", guide)
        self.assertIn("`-f docker-compose.yml`", guide)
        self.assertIn("an image-only update cannot add one", guide)

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

    def test_ui_writer_owner_matches_the_default_compose_user(self) -> None:
        import yaml

        module = self.load_matrix_module()
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        user = compose["services"]["enclosure-ui"]["user"]
        self.assertEqual(user, f"{module.UI_WRITER_UID}:{module.UI_WRITER_GID}")

    def test_ui_written_file_ownership_names_observed_and_expected_owner(self) -> None:
        module = self.load_matrix_module()
        regular = 0o100644
        module._require_ui_written_file(module.UI_WRITER_UID, module.UI_WRITER_GID, regular, "alias")
        with self.assertRaisesRegex(
            RuntimeError, r"alias persistence ownership failed: owner=10001:10001 .*expected=0:0"
        ):
            module._require_ui_written_file(10001, 10001, regular, "alias")
        with self.assertRaisesRegex(RuntimeError, "regular=False"):
            module._require_ui_written_file(0, 0, 0o040755, "mapping")

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
                patch.object(module, "UI_WRITER_UID", os.getuid()),
                patch.object(module, "UI_WRITER_GID", os.getgid()),
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
            request_urls: list[str] = []
            events: list[str] = []
            prefix = ("docker", "compose", "--project-name", "matrix")
            system_id = "synthetic core/a+b"
            enclosure_id = "synthetic enclosure?x=1&y=2"
            mapping_revision = "c" * 64
            scope_query = urllib.parse.urlencode(
                (("system_id", system_id), ("enclosure_id", enclosure_id))
            )

            def require_mapping(url, expected, **kwargs):
                self.assertEqual(expected, 200)
                request_urls.append(url)
                if url.endswith("/api/inventory"):
                    return json.dumps(
                        {
                            "selected_system_id": system_id,
                            "selected_enclosure_id": enclosure_id,
                            "slots": [
                                {
                                    "slot": 0,
                                    "mapping_revision": mapping_revision,
                                }
                            ],
                        }
                    ).encode()
                if "/api/mappings/export" in url:
                    export_calls.append(kwargs)
                    return json.dumps({"revision": "a" * 64}).encode()
                if kwargs.get("method") == "POST":
                    self.assertEqual(
                        kwargs["payload"]["expected_revision"],
                        mapping_revision,
                    )
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
                patch.object(module, "UI_WRITER_UID", os.getuid()),
                patch.object(module, "UI_WRITER_GID", os.getgid()),
            ):
                module._verify_mapping_cycle(root, module.VARIANTS[0], ports, prefix)
        self.assertEqual(
            export_calls,
            [{"authenticated": True}],
        )
        self.assertEqual(
            request_urls,
            [
                "http://127.0.0.1:19080/api/inventory",
                f"http://127.0.0.1:19080/api/mappings/export?{scope_query}",
                f"http://127.0.0.1:19080/api/slots/0/mapping?{scope_query}",
                f"http://127.0.0.1:19080/api/slots/0/mapping?{scope_query}"
                f"&expected_revision={'b' * 64}",
            ],
        )
        self.assertEqual(len(clear_urls), 1)
        for url in request_urls[1:]:
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query,
                keep_blank_values=True,
            )
            self.assertEqual(query["system_id"], [system_id])
            self.assertEqual(query["enclosure_id"], [enclosure_id])
        clear_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(clear_urls[0]).query,
            keep_blank_values=True,
        )
        self.assertEqual(
            clear_query,
            {
                "system_id": [system_id],
                "enclosure_id": [enclosure_id],
                "expected_revision": ["b" * 64],
            },
        )
        self.assertEqual(events, ["save", "restart", "clear"])

    def test_mapping_cycle_stops_when_physical_scope_is_unavailable(self) -> None:
        module = self.load_matrix_module()
        ports = module.Ports(19080, 19081, 19082)
        prefix = ("docker", "compose", "--project-name", "matrix")
        invalid_scopes = (
            {"selected_enclosure_id": "synthetic-enclosure"},
            {"selected_system_id": None, "selected_enclosure_id": "synthetic-enclosure"},
            {"selected_system_id": "", "selected_enclosure_id": "synthetic-enclosure"},
            {"selected_system_id": 7, "selected_enclosure_id": "synthetic-enclosure"},
            {"selected_system_id": "synthetic-core", "selected_enclosure_id": ""},
            {"selected_system_id": "synthetic-core", "selected_enclosure_id": 7},
        )
        for inventory in invalid_scopes:
            with self.subTest(inventory=inventory), tempfile.TemporaryDirectory() as temp_dir:
                with (
                    patch.object(
                        module,
                        "_require_status",
                        return_value=json.dumps(inventory).encode(),
                    ) as require_status,
                    self.assertRaisesRegex(RuntimeError, "physical mapping scope is unavailable"),
                ):
                    module._verify_mapping_cycle(
                        Path(temp_dir),
                        module.VARIANTS[0],
                        ports,
                        prefix,
                    )
                require_status.assert_called_once_with(
                    "http://127.0.0.1:19080/api/inventory",
                    200,
                    authenticated=True,
                )

    def test_mapping_cycle_uses_system_scope_when_no_enclosure_is_selected(self) -> None:
        module = self.load_matrix_module()
        ports = module.Ports(19080, 19081, 19082)
        prefix = ("docker", "compose", "--project-name", "matrix")
        request_urls: list[str] = []
        inventory: dict[str, object] = {}

        def stop_after_export(url, expected, **kwargs):
            request_urls.append(url)
            if url.endswith("/api/inventory"):
                return json.dumps(inventory).encode()
            raise RuntimeError("stop after scope check")

        for inventory_extra in ({}, {"selected_enclosure_id": None}):
            request_urls.clear()
            inventory.clear()
            inventory.update(
                {
                    "selected_system_id": "synthetic-core",
                    "slots": [{"slot": 0, "mapping_revision": "c" * 64}],
                    **inventory_extra,
                }
            )
            with self.subTest(inventory_extra=inventory_extra), tempfile.TemporaryDirectory() as temp_dir:
                with (
                    patch.object(module, "_require_status", side_effect=stop_after_export),
                    self.assertRaisesRegex(RuntimeError, "stop after scope check"),
                ):
                    module._verify_mapping_cycle(Path(temp_dir), module.VARIANTS[0], ports, prefix)
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(request_urls[1]).query)
                self.assertEqual(query, {"system_id": ["synthetic-core"]})

    def test_mapping_cycle_stops_when_slot_save_revision_is_unavailable(self) -> None:
        module = self.load_matrix_module()
        ports = module.Ports(19080, 19081, 19082)
        prefix = ("docker", "compose", "--project-name", "matrix")
        inventory = {
            "selected_system_id": "synthetic-core",
            "selected_enclosure_id": "synthetic-enclosure",
            "slots": [{"slot": 0, "mapping_revision": None}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(
                    module,
                    "_require_status",
                    return_value=json.dumps(inventory).encode(),
                ) as require_status,
                self.assertRaisesRegex(RuntimeError, "slot save revision is unavailable"),
            ):
                module._verify_mapping_cycle(
                    Path(temp_dir),
                    module.VARIANTS[0],
                    ports,
                    prefix,
                )
        require_status.assert_called_once_with(
            "http://127.0.0.1:19080/api/inventory",
            200,
            authenticated=True,
        )

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
        for down_code, remove_code in ((1, 0), (0, 1)):
            with self.subTest(down=down_code), patch.object(module, "_assert_compose_resources_removed") as readback:
                with patch.object(module.subprocess, "run", side_effect=[Mock(returncode=down_code), Mock(returncode=remove_code)]) as run:
                    with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                        module._cleanup_variant(prefix, root)
                readback.assert_called_once()
                self.assertEqual(run.call_count, 1 if down_code else 2)

    def test_variant_cleanup_readback_requires_reserved_resources_absent(self) -> None:
        module = self.load_matrix_module()
        with patch.object(module.subprocess, "run", return_value=Mock(returncode=0, stdout="")) as run:
            module._assert_compose_resources_removed("tjui-matrix-ui-only")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[1] for command in commands], ["container", "container", "network", "volume"])
        self.assertTrue(all("label=com.docker.compose.project=tjui-matrix-ui-only" in c for c in commands[1:]))
        for result in (Mock(returncode=1, stdout=""), Mock(returncode=0, stdout="truenas-jbod-ui")):
            with patch.object(module.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "cleanup readback"):
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

    def test_runtime_root_cleanup_failure_does_not_mask_the_matrix_failure(self) -> None:
        module = self.load_matrix_module()

        class VariantFailure(RuntimeError):
            pass

        runtime_root = Path("/synthetic-scratch/compose-matrix-runtime")
        args = Mock(
            ack="I_APPROVE_DISPOSABLE_COMPOSE_QA",
            image="sha256:" + "a" * 64,
            source_commit="b" * 40,
            ui_port=19080,
            history_port=19081,
            admin_port=19082,
            scratch_root=Mock(),
            runtime_root=runtime_root,
            compose=Path("docker-compose.yml"),
            config_fixture=Path("ci-smoke-config.yaml"),
        )
        stderr = io.StringIO()
        with (
            patch.object(module, "parse_args", return_value=args),
            patch.object(module, "validate_exact_image"),
            patch.object(module, "validate_ports", return_value=(19080, 19081, 19082)),
            patch.object(module, "validate_ports_available"),
            patch.object(module, "_read_available_memory_kib", return_value=1),
            patch.object(module, "validate_available_memory"),
            patch.object(module.shutil, "disk_usage", return_value=Mock(free=1)),
            patch.object(module, "validate_free_disk"),
            patch.object(module, "_validate_container_names_available"),
            patch.object(module, "_require_regular_file", side_effect=lambda path, label: path),
            patch.object(module, "validate_runtime_root", return_value=runtime_root),
            patch.object(
                module,
                "_run_variant",
                side_effect=VariantFailure("mapping readback mismatch"),
            ),
            patch.object(module.subprocess, "run", return_value=Mock(returncode=1)),
        ):
            with self.assertRaises(VariantFailure), contextlib.redirect_stderr(stderr):
                module.main()

        self.assertIn("compose matrix runtime-root cleanup", stderr.getvalue())

    def _synthetic_controller(self, *, failure="", variant_index=0):
        """Execute main and variant lifecycle with only OS/HTTP transport replaced."""
        import shutil
        from types import SimpleNamespace

        module = self.load_matrix_module()
        with tempfile.TemporaryDirectory() as temp:
            scratch = Path(temp)
            scratch.chmod(0o700)
            runtime = scratch / "runtime"
            variant = module.VARIANTS[variant_index]
            project = f"tjui-matrix-{variant.name}"
            image, revision = "sha256:" + "a" * 64, "b" * 40
            args = SimpleNamespace(
                ack="I_APPROVE_DISPOSABLE_COMPOSE_QA", image=image, source_commit=revision,
                ui_port=19080, history_port=19081, admin_port=19082,
                scratch_root=scratch, runtime_root=runtime,
                compose=ROOT / "docker-compose.yml", config_fixture=ROOT / "config" / "config.example.yaml",
            )
            events, children = [], []
            original = RuntimeError("original variant failure")
            state = {"started": False, "down": False}
            expected_services = set(variant.services)
            services = {}
            port_map = {"enclosure-ui": (19080, 8000), "enclosure-history": (19081, 8001), "enclosure-admin": (19082, 8002)}
            for service in expected_services | ({"enclosure-ui"} if variant.admin_initial_setup else set()):
                env = {"BACKUP_TARGETS_JSON": "[]", "BACKUP_FULL_ENABLED": "false", "METRICS_PATH": "/metrics"}
                if service in {"enclosure-ui", "enclosure-admin"}:
                    env.update(ADMIN_AUTH_MODE="basic", ADMIN_AUTH_USERNAME=module.AUTH_USERNAME,
                               ADMIN_AUTH_PASSWORD=module.AUTH_PASSWORD, READ_UI_AUTH_MODE="basic",
                               READ_UI_AUTH_USERNAME=module.AUTH_USERNAME, READ_UI_AUTH_PASSWORD=module.AUTH_PASSWORD)
                services[service] = {
                    "image": image, "environment": env,
                    "volumes": [{"type": "bind", "source": str(runtime / variant.name / "data"), "target": "/app/data"}],
                    "ports": [{"host_ip": "127.0.0.1", "published": str(port_map[service][0]),
                               "target": port_map[service][1], "protocol": "tcp"}] if service in port_map else [],
                }
            volumes = {}
            if "enclosure-admin" in services:
                volumes["host-prep-staging"] = {"name": project + "_host-prep-staging", "driver": "local"}
                services["enclosure-admin"]["volumes"].append({"type": "volume", "source": "host-prep-staging", "target": "/app/host-prep"})
            if failure == "render-volume-driver":
                volumes["host-prep-staging"]["driver"] = "unreviewed-remote-plugin"
            if failure == "render-image":
                services[variant.services[0]]["image"] = "unreviewed:latest"
            if failure == "render-port":
                services[variant.services[0]]["ports"][0]["host_ip"] = "0.0.0.0"
            if failure == "render-mount":
                services[variant.services[0]]["volumes"][0]["source"] = str(scratch / "outside")
            if failure == "render-target":
                services[variant.services[0]]["environment"]["BACKUP_TARGETS_JSON"] = '[{"type":"sftp"}]'
            if failure == "render-auth":
                services[variant.services[0]]["environment"]["ADMIN_AUTH_MODE"] = "network"

            def prepare(root, **kwargs):
                root.mkdir()
                (root / "compose.yaml").write_text("synthetic recovery configuration")
                module._write_environment(root, image, kwargs["ports"], variant=variant)

            def transport(command, **kwargs):
                command = list(command)
                children.append((command, kwargs))
                if command[:3] == ["docker", "image", "inspect"]:
                    return Mock(returncode=0, stdout=f"{image}\n{revision}\n")
                if command[:2] == ["sudo", "rm"]:
                    events.append("remove")
                    shutil.rmtree(command[-1])
                    return Mock(returncode=0, stdout="")
                if "compose" in command:
                    if "config" in command:
                        events.append("config")
                        return Mock(returncode=0, stdout=json.dumps({"services": services, "volumes": volumes}))
                    if "up" in command:
                        events.append("up")
                        state["started"] = True
                        if variant.admin_initial_setup and command[-1] == "enclosure-ui":
                            expected_services.add("enclosure-ui")
                        if failure.startswith("cleanup-"):
                            raise original
                    if "down" in command:
                        events.append("down")
                        state["down"] = True
                        if failure in {"cleanup-down", "cleanup-down-absent"}:
                            return Mock(returncode=1, stdout="")
                    if "ps" in command:
                        if "--services" in command:
                            return Mock(returncode=0, stdout="\n".join(expected_services))
                        if failure == "cleanup-diagnostics":
                            raise RuntimeError("diagnostics must not mask original")
                    return Mock(returncode=0, stdout="")
                if "inspect" in command and state["down"]:
                    events.append("readback")
                    return Mock(returncode=0 if failure in {"cleanup-down", "cleanup-residual"} else 2 if failure == "cleanup-unknown" else 1, stdout="")
                if "container" in command and "inspect" in command:
                    events.append("inspect")
                    records = []
                    for service, spec in services.items():
                        if service not in expected_services:
                            continue
                        ports = {f"{p['target']}/tcp": [{"HostIp": p["host_ip"], "HostPort": p["published"]}] for p in spec["ports"]}
                        record = {"Image": image, "State": {"Running": True},
                                  "Config": {"Labels": {"com.docker.compose.project": project,
                                             "com.docker.compose.service": service,
                                             "org.opencontainers.image.revision": revision},
                                             "Env": [f"{k}={v}" for k, v in spec["environment"].items()]},
                                  "Mounts": [{"Type": m["type"], "Source": m["source"],
                                              "Name": volumes[m["source"]]["name"] if m["type"] == "volume" else None,
                                              "Destination": m["target"], "RW": True} for m in spec["volumes"]],
                                  "HostConfig": {"PortBindings": ports or None, "NetworkMode": project + "_default"},
                                  "NetworkSettings": {"Ports": ports}}
                        if failure == "runtime-image":
                            record["Image"] = "sha256:" + "c" * 64
                        if failure == "runtime-revision":
                            record["Config"]["Labels"]["org.opencontainers.image.revision"] = "c" * 40
                        if failure == "runtime-port":
                            record["NetworkSettings"]["Ports"] = {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "19080"}]}
                        if failure == "runtime-mount":
                            record["Mounts"][0]["Source"] = str(scratch / "outside")
                        if failure == "runtime-target":
                            record["Config"]["Env"].append('BACKUP_TARGETS_JSON=[{"type":"sftp"}]')
                        records.append(record)
                    return Mock(returncode=0, stdout=json.dumps(records))
                if command[:2] == ["docker", "container"]:
                    if not state["started"]:
                        return Mock(returncode=0, stdout="preexisting" if failure == "preexisting" and "--filter" in command else "")
                    if state["down"]:
                        events.append("readback")
                        if failure == "cleanup-unknown":
                            return Mock(returncode=1, stdout="")
                        return Mock(returncode=0, stdout="residual" if failure in {"cleanup-down", "cleanup-residual"} else "")
                    return Mock(returncode=0, stdout="\n".join("id-" + s for s in expected_services))
                if "network" in command or "volume" in command:
                    events.append("readback")
                return Mock(returncode=0, stdout="")

            hostile = {"DOCKER_HOST": "tcp://example.test:2375", "COMPOSE_FILE": "/unreviewed/compose.yml",
                       "COMPOSE_PROFILES": "backup", "JBOD_UI_IMAGE": "unreviewed:latest",
                       "APP_BIND_ADDRESS": "0.0.0.0", "ADMIN_AUTH_MODE": "network",
                       "BACKUP_TARGETS_JSON": '[{"type":"sftp"}]', "HISTORY_BACKUP_DIR": "/unreviewed"}
            error = None
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, hostile))
                stack.enter_context(patch.object(module, "parse_args", return_value=args))
                stack.enter_context(patch.object(module, "VARIANTS", (variant,)))
                stack.enter_context(patch.object(module, "_prepare_variant_root", side_effect=prepare))
                stack.enter_context(patch.object(module.subprocess, "run", side_effect=transport))
                stack.enter_context(patch.object(module, "_require_status", return_value=b'{"ok": true, "system": {"id": "demo-builder-lab"}}'))
                stack.enter_context(patch.object(module, "_read_app_owned_text", return_value="demo-builder-lab demo-builder-lab-chassis"))
                for name in ("validate_ports_available", "validate_available_memory", "validate_free_disk",
                             "_verify_ui", "_verify_history", "_verify_admin", "_verify_pencil_cycle",
                             "_verify_mapping_cycle", "_verify_scheduler_disabled", "_verify_scheduler_enabled"):
                    stack.enter_context(patch.object(module, name))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                try:
                    module.main()
                except BaseException as exc:
                    error = exc
            retained = (runtime / variant.name / "compose.yaml").exists()
            environment = (runtime / variant.name / ".env").read_text() if retained else ""
            return error, original, events, children, retained, environment, stderr.getvalue()

    def test_main_retains_recovery_config_until_verified_absence(self) -> None:
        for failure in ("cleanup-down", "cleanup-down-absent", "cleanup-residual", "cleanup-unknown", "cleanup-absent", "cleanup-diagnostics"):
            with self.subTest(failure=failure):
                error, original, events, _, retained, environment, stderr = self._synthetic_controller(failure=failure)
                self.assertIs(error, original)
                self.assertIn("down", events)
                self.assertIn("readback", events)
                expected_retained = failure in {"cleanup-down", "cleanup-down-absent", "cleanup-residual", "cleanup-unknown"}
                self.assertEqual(retained, expected_retained)
                if expected_retained:
                    self.assertNotIn("remove", events)
                    self.assertIn("JBOD_UI_IMAGE=sha256:", environment)
                    self.assertIn("retained", stderr)
                else:
                    self.assertLess(events.index("readback"), events.index("remove"))

    def test_main_constrains_all_children_and_verifies_actual_runtime(self) -> None:
        for variant_index in range(7):
            with self.subTest(variant=variant_index):
                error, _, events, children, retained, _, _ = self._synthetic_controller(variant_index=variant_index)
                self.assertIsNone(error)
                self.assertFalse(retained)
                self.assertIn("config", events)
                self.assertIn("inspect", events)
                self.assertLess(events.index("config"), events.index("up"))
                for command, kwargs in children:
                    env = kwargs.get("env")
                    self.assertIsInstance(env, dict, command)
                    self.assertNotIn("COMPOSE_FILE", env)
                    self.assertNotIn("BACKUP_TARGETS_JSON", env)
                    self.assertNotIn("HISTORY_BACKUP_DIR", env)
                    self.assertNotIn("JBOD_UI_IMAGE", env)
                    self.assertEqual(env["DOCKER_HOST"], "unix:///var/run/docker.sock")
                    if "compose" in command:
                        self.assertIn("--env-file", command)

    def test_main_refuses_preexisting_project_without_teardown(self) -> None:
        error, _, events, _, _, _, _ = self._synthetic_controller(failure="preexisting")
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIn("up", events)
        self.assertNotIn("down", events)

    def test_main_refuses_rendered_and_runtime_contract_drift(self) -> None:
        for failure in ("render-image", "render-port", "render-mount", "render-target", "render-auth",
                        "runtime-image", "runtime-revision", "runtime-port", "runtime-mount", "runtime-target", "render-volume-driver"):
            with self.subTest(failure=failure):
                error, _, events, _, retained, _, _ = self._synthetic_controller(failure=failure, variant_index=3 if failure == "render-volume-driver" else 0)
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("contract", str(error))
                if failure.startswith("render-"):
                    self.assertNotIn("up", events)
                else:
                    self.assertIn("inspect", events)
                self.assertFalse(retained)

    def test_cleanup_failure_raises_when_it_is_the_first_failure(self) -> None:
        module = self.load_matrix_module()

        def failing_cleanup() -> None:
            raise RuntimeError("synthetic cleanup failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
            module._cleanup_after_run(failing_cleanup, "synthetic cleanup")

    def test_cleanup_failure_is_reported_and_suppressed_while_another_error_propagates(self) -> None:
        module = self.load_matrix_module()

        class BodyFailure(RuntimeError):
            pass

        def failing_cleanup() -> None:
            raise RuntimeError("synthetic cleanup failure")

        stderr = io.StringIO()
        with self.assertRaises(BodyFailure), contextlib.redirect_stderr(stderr):
            try:
                raise BodyFailure("original failure")
            finally:
                module._cleanup_after_run(failing_cleanup, "synthetic cleanup")

        report = stderr.getvalue()
        self.assertIn("synthetic cleanup", report)
        self.assertIn("synthetic cleanup failure", report)


if __name__ == "__main__":
    unittest.main()
