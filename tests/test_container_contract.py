from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dev.yml")
SUPPORTED_COMPOSE_FILES = (*COMPOSE_FILES, "docker-compose.secrets.yml", "docker-compose.nonroot.yml")
EXPECTED_COMPOSE_SECRETS = {
    "truenas_api_key": "TRUENAS_API_KEY_FILE",
    "truenas_api_password": "TRUENAS_API_PASSWORD_FILE",
    "ssh_password": "SSH_PASSWORD_FILE",
    "ssh_sudo_password": "SSH_SUDO_PASSWORD_FILE",
    "admin_auth_password": "ADMIN_AUTH_PASSWORD_FILE",
}
EXPECTED_MEMORY_LIMITS = {
    "enclosure-ui": "${APP_MEM_LIMIT:-1g}",
    "enclosure-history": "${HISTORY_MEM_LIMIT:-1g}",
    "enclosure-admin": "${ADMIN_MEM_LIMIT:-3g}",
    "enclosure-backup": "${BACKUP_MEM_LIMIT:-3g}",
}
EXPECTED_HISTORY_PERMISSION_ENV = {
    "HISTORY_PERMISSION_REPAIR_ENABLED": "${HISTORY_PERMISSION_REPAIR_ENABLED:-false}",
    "HISTORY_SHARED_DIR_MODE": "${HISTORY_SHARED_DIR_MODE:-0770}",
    "HISTORY_SHARED_FILE_MODE": "${HISTORY_SHARED_FILE_MODE:-0660}",
}
SEGMENTED_HISTORY_CLI_PATHS = (
    "scripts/migrate_segmented_history.py",
    "scripts/rotate_segmented_history.py",
    "scripts/query_segmented_history.py",
    "scripts/seal_history_segment.py",
)


def writable_volume_targets(service: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for volume in service.get("volumes", []):
        if not isinstance(volume, str):
            continue
        fields = volume.split(":")
        if len(fields) < 2 or (len(fields) > 2 and fields[-1] == "ro"):
            continue
        targets.add(fields[1])
    return targets


class ContainerResourceContractTests(unittest.TestCase):
    def test_documented_segmented_history_cli_sources_exist(self) -> None:
        runbook = (REPO_ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
        documented_paths = set(
            re.findall(r"`(scripts/(?:migrate|rotate|query|seal)[^`]+\.py)`", runbook)
        )

        self.assertEqual(documented_paths, set(SEGMENTED_HISTORY_CLI_PATHS))
        for relative_path in documented_paths:
            with self.subTest(script=relative_path):
                self.assertTrue((REPO_ROOT / relative_path).is_file())

    def test_production_image_packages_documented_segmented_history_clis(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

        for relative_path in SEGMENTED_HISTORY_CLI_PATHS:
            with self.subTest(script=relative_path):
                self.assertIn(
                    f"COPY {relative_path} /app/{relative_path}",
                    dockerfile,
                )
        self.assertNotIn("COPY scripts /app/scripts", dockerfile)

    def test_segmented_history_runbook_uses_compose_cli_invocations(self) -> None:
        runbook = (REPO_ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
        history_command_prefix = "docker compose run --rm --entrypoint python enclosure-history "

        self.assertNotRegex(
            runbook,
            r"(?m)^python scripts/(?:migrate|rotate)_segmented_history\.py",
        )
        self.assertEqual(
            runbook.count(f"{history_command_prefix}scripts/migrate_segmented_history.py"),
            6,
        )
        self.assertEqual(
            runbook.count(f"{history_command_prefix}scripts/rotate_segmented_history.py"),
            3,
        )

    def test_segmented_history_runbook_commands_use_available_service_paths(self) -> None:
        runbook = (REPO_ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
        command_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)
            if block.startswith("docker compose run --rm ")
        ]

        self.assertTrue(command_blocks)
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))[
                "services"
            ]
            for block in command_blocks:
                arguments = shlex.split(block.replace("\\\n", " "))
                script_index = next(
                    index
                    for index, argument in enumerate(arguments)
                    if argument.startswith("scripts/") and argument.endswith(".py")
                )
                service_name = arguments[script_index - 1]
                mounted_roots = {
                    PurePosixPath(volume.split(":")[1])
                    for volume in services[service_name].get("volumes", [])
                    if isinstance(volume, str) and len(volume.split(":")) >= 2
                }
                referenced_paths = {
                    PurePosixPath(argument)
                    for argument in arguments
                    if argument.startswith("/app/")
                }
                for referenced_path in referenced_paths:
                    with self.subTest(
                        compose=compose_name,
                        service=service_name,
                        path=str(referenced_path),
                    ):
                        self.assertTrue(
                            any(
                                referenced_path == root or root in referenced_path.parents
                                for root in mounted_roots
                            ),
                            f"{referenced_path} is not available to {service_name}",
                        )

    def test_rotation_runbook_does_not_cross_the_split_backup_and_history_identities(self) -> None:
        runbook = (REPO_ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
        backup_guide = (
            REPO_ROOT / "wiki/Backup-Restore-and-Debug-Bundles.md"
        ).read_text(encoding="utf-8")
        rotation_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)
            if "scripts/rotate_segmented_history.py" in block
        ]

        self.assertRegex(
            backup_guide,
            r"Archives and the passphrase remain private `0600`\s+files",
        )
        self.assertEqual(len(rotation_blocks), 3)
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))[
                "services"
            ]
            with self.subTest(compose=compose_name):
                self.assertNotEqual(
                    services["enclosure-history"]["user"],
                    services["enclosure-backup"]["user"],
                )
        for block in rotation_blocks:
            arguments = shlex.split(block.replace("\\\n", " "))
            script_index = arguments.index("scripts/rotate_segmented_history.py")
            with self.subTest(command=arguments[-1]):
                self.assertEqual(arguments[script_index - 1], "enclosure-history")
                self.assertNotIn("--user", arguments)
                self.assertNotIn("/app/backups", arguments)
                if "--recover" not in arguments:
                    backup_dir_index = arguments.index("--scheduled-backup-dir")
                    status_index = arguments.index("--scheduled-backup-status")
                    self.assertEqual(
                        arguments[backup_dir_index + 1],
                        "/app/history/.segment-rotation-backup",
                    )
                    self.assertEqual(
                        arguments[status_index + 1],
                        "/app/backup-status/scheduled-backup.json",
                    )

    def test_rotation_staging_copies_only_status_named_archive_for_the_app_owner(self) -> None:
        runbook = (REPO_ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
        staging_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)
            if "staged_backup=ok" in block
        ]

        self.assertEqual(len(staging_blocks), 1)
        marker = "python3 - <<'PY'\n"
        self.assertIn(marker, staging_blocks[0])
        staging_script = staging_blocks[0].split(marker, 1)[1].rsplit("\nPY", 1)[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_dir = root / "backup-status"
            backup_dir = root / "backups" / "scheduled"
            history_dir = root / "history"
            status_dir.mkdir(mode=0o750)
            backup_dir.mkdir(parents=True, mode=0o700)
            history_dir.mkdir(mode=0o700)
            artifact_name = "jbod-scheduled-backup-20300102T030405Z-1234abcd.7z"
            artifact_bytes = b"synthetic-private-encrypted-backup"
            artifact = backup_dir / artifact_name
            artifact.write_bytes(artifact_bytes)
            artifact.chmod(0o600)
            unrelated_archive = backup_dir / "jbod-scheduled-backup-20300101T030405Z-deadbeef.7z"
            unrelated_archive.write_bytes(b"not-selected")
            unrelated_archive.chmod(0o600)
            passphrase = root / "backups" / "scheduled-backup-passphrase"
            passphrase.write_bytes(b"not-copied")
            passphrase.chmod(0o600)
            status = {
                "schema_version": 1,
                "enabled": True,
                "included_groups": ["config_file", "history_db"],
                "success_count": 1,
                "failure_count": 0,
                "last_attempt_at": "2030-01-02T03:04:05+00:00",
                "last_success_at": "2030-01-02T03:04:05+00:00",
                "last_failure_at": None,
                "last_size_bytes": len(artifact_bytes),
                "last_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                "last_artifact_name": artifact_name,
                "last_absent_groups": [],
                "last_retention_removed": 0,
                "last_error_code": None,
            }
            status_path = status_dir / "scheduled-backup.json"
            status["last_sha256"] = "0" * 64
            status_path.write_text(json.dumps(status), encoding="utf-8")
            status_path.chmod(0o640)
            identity = str(os.getuid())
            group = str(os.getgid())
            environment = {
                **os.environ,
                "APP_UID": identity,
                "APP_GID": group,
                "BACKUP_UID": identity,
                "BACKUP_GID": group,
            }

            rejected = subprocess.run(
                ["python3", "-c", staging_script],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((history_dir / ".segment-rotation-backup").exists())
            self.assertNotIn(artifact_name, rejected.stdout + rejected.stderr)
            self.assertNotIn("0" * 64, rejected.stdout + rejected.stderr)

            status["last_sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
            status_path.write_text(json.dumps(status), encoding="utf-8")
            status_path.chmod(0o640)
            completed = subprocess.run(
                ["python3", "-c", staging_script],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "staged_backup=ok\n")
            self.assertEqual(completed.stderr, "")
            stage_dir = history_dir / ".segment-rotation-backup"
            self.assertEqual(stat.S_IMODE(stage_dir.stat().st_mode), 0o700)
            self.assertEqual((stage_dir.stat().st_uid, stage_dir.stat().st_gid), (os.getuid(), os.getgid()))
            staged_files = list(stage_dir.iterdir())
            self.assertEqual([path.name for path in staged_files], [artifact_name])
            self.assertEqual(staged_files[0].read_bytes(), artifact_bytes)
            self.assertEqual(stat.S_IMODE(staged_files[0].stat().st_mode), 0o600)
            self.assertEqual(
                (staged_files[0].stat().st_uid, staged_files[0].stat().st_gid),
                (os.getuid(), os.getgid()),
            )

            staged_files[0].unlink()
            repeated = subprocess.run(
                ["python3", "-c", staging_script],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(repeated.returncode, 0)
            self.assertTrue(stage_dir.is_dir())
            self.assertEqual(list(stage_dir.iterdir()), [])

    def test_rotation_staging_cleanup_is_bounded_to_the_fixed_directory(self) -> None:
        runbook = (REPO_ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
        cleanup_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)
            if "rm -rf" in block and ".segment-rotation-backup" in block
        ]

        self.assertEqual(len(cleanup_blocks), 1)
        cleanup_arguments = shlex.split(cleanup_blocks[0].replace("\\\n", " "))
        self.assertEqual(
            cleanup_arguments,
            [
                "sudo",
                "rm",
                "-rf",
                "--one-file-system",
                "--",
                "history/.segment-rotation-backup",
            ],
        )
        self.assertRegex(
            runbook,
            r"(?i)after reviewing[^.]+result[^.]+remove[^.]+fixed staging directory",
        )

    def test_history_reads_scheduled_backup_status_for_segmented_retention(self) -> None:
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))[
                "services"
            ]
            history = services["enclosure-history"]
            with self.subTest(compose=compose_name):
                self.assertEqual(
                    history["environment"]["SCHEDULED_BACKUP_STATUS_FILE"],
                    "${SCHEDULED_BACKUP_STATUS_FILE:-}",
                )
                self.assertEqual(
                    history["environment"]["HISTORY_SEGMENTED_BACKUP_MAX_AGE_SECONDS"],
                    "${HISTORY_SEGMENTED_BACKUP_MAX_AGE_SECONDS:-129600}",
                )
                self.assertIn("./backup-status:/app/backup-status:ro", history["volumes"])

    def test_history_capable_services_keep_segment_catalog_opt_in(self) -> None:
        expected = "${HISTORY_SEGMENT_CATALOG_PATH:-}"
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))[
                "services"
            ]
            for service_name in (
                "enclosure-history",
                "enclosure-admin",
                "enclosure-backup",
            ):
                with self.subTest(compose=compose_name, service=service_name):
                    self.assertEqual(
                        services[service_name]["environment"]["HISTORY_SEGMENT_CATALOG_PATH"],
                        expected,
                    )

        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        active_assignments = [
            line.strip()
            for line in env_example.splitlines()
            if line.strip().startswith("HISTORY_SEGMENT_CATALOG_PATH=")
        ]
        self.assertEqual(active_assignments, [])

    def test_ui_and_history_are_nonroot_by_default_with_compatible_overlay(self) -> None:
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))["services"]
            with self.subTest(compose=compose_name):
                self.assertEqual(
                    services["enclosure-ui"]["user"],
                    "${APP_UID:-10001}:${APP_GID:-10001}",
                )
                self.assertEqual(
                    services["enclosure-history"]["user"],
                    "${APP_UID:-10001}:${APP_GID:-10001}",
                )
                self.assertEqual(
                    services["enclosure-admin"]["user"],
                    "0:${APP_GID:-10001}",
                )
                self.assertEqual(
                    services["enclosure-backup"]["user"],
                    "${BACKUP_UID:-1000}:${BACKUP_GID:-1000}",
                )
                self.assertEqual(
                    services["enclosure-backup"]["environment"]["APP_GID"],
                    "${APP_GID:-10001}",
                )
                self.assertEqual(
                    services["enclosure-backup"]["group_add"],
                    ["${APP_GID:-10001}"],
                )
                self.assertIn("./config:/app/config:ro", services["enclosure-ui"]["volumes"])

        overlay = yaml.safe_load((REPO_ROOT / "docker-compose.nonroot.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            set(overlay["services"]),
            {"enclosure-ui", "enclosure-history", "enclosure-backup"},
        )
        self.assertEqual(overlay["services"]["enclosure-ui"]["user"], "${APP_UID:-10001}:${APP_GID:-10001}")
        self.assertEqual(
            overlay["services"]["enclosure-history"]["user"],
            "${APP_UID:-10001}:${APP_GID:-10001}",
        )
        self.assertIn("./config:/app/config:ro", overlay["services"]["enclosure-ui"]["volumes"])
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG APP_UID=10001", dockerfile)
        self.assertIn("ARG APP_GID=10001", dockerfile)
        self.assertIn("USER app", dockerfile)

    def test_admin_public_origin_reaches_every_compose_admin_service(self) -> None:
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load(
                (REPO_ROOT / compose_name).read_text(encoding="utf-8")
            )["services"]
            self.assertEqual(
                services["enclosure-admin"]["environment"]["ADMIN_PUBLIC_ORIGIN"],
                "${ADMIN_PUBLIC_ORIGIN:-}",
                compose_name,
            )

    def test_compose_ui_port_supports_an_explicit_loopback_bind(self) -> None:
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load(
                (REPO_ROOT / compose_name).read_text(encoding="utf-8")
            )["services"]
            self.assertEqual(
                services["enclosure-ui"]["ports"],
                ["${APP_BIND_ADDRESS:-0.0.0.0}:${APP_PORT:-8080}:8000"],
                compose_name,
            )

    def test_image_carries_source_revision_label(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG SOURCE_COMMIT=unknown", dockerfile)
        self.assertIn(
            "LABEL org.opencontainers.image.revision=$SOURCE_COMMIT",
            dockerfile,
        )
        dev_compose = yaml.safe_load(
            (REPO_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
        )
        for service_name, service in dev_compose["services"].items():
            self.assertEqual(
                service["build"].get("args", {}).get("SOURCE_COMMIT"),
                "${SOURCE_COMMIT:-unknown}",
                service_name,
            )

    def test_compose_services_use_read_only_root_filesystems_and_drop_privileges(self) -> None:
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))["services"]
            for service_name, service in services.items():
                with self.subTest(compose=compose_name, service=service_name):
                    self.assertIs(service.get("read_only"), True)
                    self.assertEqual(service.get("tmpfs"), ["/tmp"])
                    self.assertEqual(service.get("cap_drop"), ["ALL"])
                    self.assertEqual(service.get("security_opt"), ["no-new-privileges:true"])

                    if service_name == "enclosure-admin":
                        self.assertEqual(service.get("cap_add"), ["CHOWN", "FOWNER"])
                        self.assertNotIn("group_add", service)
                    else:
                        self.assertNotIn("cap_add", service)

    def test_compose_writable_mounts_are_limited_to_service_state(self) -> None:
        expected_targets = {
            "enclosure-ui": {"/app/data", "/app/logs"},
            "enclosure-history": {"/app/history"},
            "enclosure-admin": {
                "/app/config",
                "/app/data",
                "/app/history",
                "/var/run/docker.sock",
            },
            "enclosure-backup": {
                "/app/history",
                "/app/backups",
                "/app/backup-status",
            },
        }
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))["services"]
            for service_name, expected in expected_targets.items():
                with self.subTest(compose=compose_name, service=service_name):
                    self.assertEqual(writable_volume_targets(services[service_name]), expected)
            self.assertNotIn("./logs:/app/logs", services["enclosure-admin"]["volumes"])

    def test_large_temporary_workspaces_use_disk_backed_state_mounts(self) -> None:
        expected_temp_roots = {
            "enclosure-history": "/app/history",
            "enclosure-admin": "/app/history",
            "enclosure-backup": "/app/backups",
        }
        for compose_name in COMPOSE_FILES:
            services = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))["services"]
            for service_name, expected_temp_root in expected_temp_roots.items():
                with self.subTest(compose=compose_name, service=service_name):
                    service = services[service_name]
                    self.assertEqual(service["environment"].get("TMPDIR"), expected_temp_root)
                    self.assertIn(expected_temp_root, writable_volume_targets(service))

        backup_guide = (
            REPO_ROOT / "wiki/Backup-Restore-and-Debug-Bundles.md"
        ).read_text(encoding="utf-8")
        self.assertIn("disk-backed scratch", backup_guide)
        self.assertIn("TMPDIR", backup_guide)

    def test_default_nonroot_migration_is_documented_before_start(self) -> None:
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        deployment_guide = (
            REPO_ROOT / "wiki/Docker-and-GHCR-Deployment.md"
        ).read_text(encoding="utf-8")
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        quick_start = (REPO_ROOT / "wiki/Quick-Start.md").read_text(encoding="utf-8")

        self.assertIn("default non-root UI and history services", env_example)
        self.assertIn("Default non-root runtime", deployment_guide)
        self.assertRegex(deployment_guide, r"before\s+the first start on the non-root Compose file")
        self.assertNotIn("v0.22.3", deployment_guide)
        self.assertNotIn("v0.22.3", quick_start)
        self.assertIn("prepare_nonroot_bind_mounts.py", readme)
        self.assertIn("prepare_nonroot_bind_mounts.py", quick_start)
        self.assertIn("--apply", quick_start)
        self.assertNotIn("The base Compose file keeps the existing root-compatible", deployment_guide)

    def test_nonroot_overlay_preserves_backup_identity_with_app_data_group(self) -> None:
        overlay = yaml.safe_load(
            (REPO_ROOT / "docker-compose.nonroot.yml").read_text(encoding="utf-8")
        )

        backup = overlay["services"]["enclosure-backup"]
        self.assertNotIn("user", backup)
        self.assertEqual(backup["group_add"], ["${APP_GID:-10001}"])

        backup_guide = (
            REPO_ROOT / "wiki/Backup-Restore-and-Debug-Bundles.md"
        ).read_text(encoding="utf-8")
        self.assertIn('-g "$APP_GID" -m 2750 backup-status', backup_guide)
        self.assertIn("Status files use `0640`", backup_guide)
        self.assertIn("segment directory uses exact mode `0750`", backup_guide)
        self.assertIn("segments and `catalog.json` use exact mode `0640`", backup_guide)

    def test_nonroot_migration_helper_is_bounded_no_follow_and_dry_run_by_default(self) -> None:
        helper = (REPO_ROOT / "scripts/prepare_nonroot_bind_mounts.py").read_text(encoding="utf-8")
        self.assertIn('APP_RECURSIVE_ROOTS = ("data", "history", "logs")', helper)
        self.assertIn('APP_CONFIG_PATHS = ("config.yaml", "ssh", "tls")', helper)
        self.assertIn("resource.getrlimit(resource.RLIMIT_NOFILE)", helper)
        self.assertIn("apply_ownership", helper)
        self.assertIn("follow_symlinks=False", helper)
        self.assertIn("O_NOFOLLOW", helper)
        self.assertIn('action="store_true"', helper)
        self.assertIn("stale replacement artifact", helper)

    def test_container_smoke_uses_real_owned_bind_mounts_for_nonroot_paths(self) -> None:
        fixture = (REPO_ROOT / "tests/fixtures/ci-smoke.compose.yml").read_text(encoding="utf-8")
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("${CI_SMOKE_DATA_DIR:?CI_SMOKE_DATA_DIR is required}:/app/data", fixture)
        self.assertIn("${CI_SMOKE_LOG_DIR:?CI_SMOKE_LOG_DIR is required}:/app/logs", fixture)
        self.assertIn("read_only: true", fixture)
        self.assertIn("- /tmp", fixture)
        self.assertIn("- ALL", fixture)
        self.assertIn('no-new-privileges:true', fixture)
        self.assertIn('"18080:8000"', fixture)
        self.assertNotIn("- /app/data", fixture)
        self.assertIn("install -d -m 0770 -o 10001 -g 10001", workflow)
        self.assertIn("assert os.getuid() == 10001", workflow)
        self.assertIn("assert os.getgid() == 10001", workflow)
        self.assertIn("data_probe.write_text", workflow)
        self.assertIn("log_probe.write_text", workflow)
        self.assertIn("temporary_probe.write_text", workflow)
        self.assertIn("rootfs_probe.write_text", workflow)
        self.assertIn('status["CapEff"] == "0000000000000000"', workflow)
        self.assertIn('status["NoNewPrivs"] == "1"', workflow)
        self.assertIn("compose_contract_root", workflow)
        self.assertIn(
            'sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" "$compose_contract_root"',
            workflow,
        )
        self.assertNotIn('mkdir -p "$compose_contract_root"', workflow)
        self.assertIn("probe_compose_service enclosure-ui 10001 10001", workflow)
        self.assertIn("probe_compose_service enclosure-history 10001 10001", workflow)
        self.assertIn("probe_compose_service enclosure-admin 0 10001", workflow)
        self.assertIn("probe_compose_service enclosure-backup 1000 1000", workflow)
        self.assertIn("EXPECTED_CAP_EFF", workflow)
        self.assertIn("tempfile.mkdtemp", workflow)
        self.assertIn("docker run --rm -i --user 1000:1000 --group-add 10001", workflow)
        self.assertGreaterEqual(workflow.count("docker run --rm -i"), 2)
        self.assertIn("backup_identity_and_group_access=ok", workflow)
        self.assertIn("ui_backup_secret_access=denied", workflow)

    def test_env_example_only_names_tracked_or_generated_compose_files(self) -> None:
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        compose_references = set(re.findall(r"\b(docker-compose(?:\.[\w-]+)?\.ya?ml)\b", env_example))

        self.assertTrue(compose_references)
        for compose_reference in compose_references:
            with self.subTest(compose=compose_reference):
                if compose_reference == "docker-compose.override.yml":
                    self.assertTrue((REPO_ROOT / f"{compose_reference}.example").is_file())
                else:
                    self.assertIn(compose_reference, SUPPORTED_COMPOSE_FILES)

    def test_quick_start_lists_clean_room_prerequisites(self) -> None:
        quick_start = (REPO_ROOT / "wiki/Quick-Start.md").read_text(encoding="utf-8")

        self.assertIn("`curl`", quick_start)
        self.assertIn("write permission", quick_start)
        self.assertIn("outbound HTTPS", quick_start)
        self.assertIn("GitHub and GHCR", quick_start)
        self.assertIn("firewall", quick_start)

    def test_source_build_guides_require_edit_before_start(self) -> None:
        for relative_path in (
            "README.md",
            "wiki/Quick-Start.md",
            "wiki/Docker-and-GHCR-Deployment.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(guide=relative_path):
                self.assertIn("Edit `.env` before the first start", guide)

    def test_source_build_guides_explain_env_precedence(self) -> None:
        for relative_path in (
            "README.md",
            "wiki/Quick-Start.md",
            "wiki/Docker-and-GHCR-Deployment.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(guide=relative_path):
                self.assertIn("values in `.env` override matching YAML settings", guide)

    def test_admin_launch_guides_repeat_trust_boundary(self) -> None:
        for relative_path in (
            "wiki/Quick-Start.md",
            "wiki/Docker-and-GHCR-Deployment.md",
            "wiki/Admin-UI-and-System-Setup.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(guide=relative_path):
                self.assertIn(
                    "https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/ADMIN_TRUST_BOUNDARY.md",
                    guide,
                )
                self.assertNotIn("../docs/", guide)
                self.assertIn("ADMIN_PUBLIC_ORIGIN", guide)
                self.assertIn("trusted operator", guide)
                self.assertIn("Docker socket", guide)
                self.assertIn("Auto-stop limits exposure; it is not authentication", guide)

    def test_main_ui_write_authorization_is_version_gated_in_published_guides(self) -> None:
        for relative_path in (
            "wiki/Quick-Start.md",
            "wiki/Docker-and-GHCR-Deployment.md",
            "wiki/Troubleshooting.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            guide_text = " ".join(guide.split())
            with self.subTest(guide=relative_path):
                self.assertIn(
                    "`v0.22.2` does not enforce `ADMIN_AUTH_MODE` on main-UI writes",
                    guide_text,
                )
                self.assertIn(
                    "Current `main` rejects these writes unless `ADMIN_AUTH_MODE=basic`",
                    guide_text,
                )

    def test_main_ui_write_control_state_matches_current_main(self) -> None:
        for relative_path in (
            "wiki/Quick-Start.md",
            "wiki/Docker-and-GHCR-Deployment.md",
            "wiki/Troubleshooting.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            guide_text = " ".join(guide.split()).lower()
            with self.subTest(guide=relative_path):
                self.assertIn("current `main` leaves the controls enabled", guide_text)
                self.assertNotIn("renders those write controls disabled", guide_text)
                self.assertNotIn("renders the write controls disabled", guide_text)
                self.assertNotIn("disables them again", guide_text)

    def test_admin_origin_startup_behavior_is_not_overstated(self) -> None:
        for relative_path in (
            "wiki/Quick-Start.md",
            "wiki/Docker-and-GHCR-Deployment.md",
            "wiki/Admin-UI-and-System-Setup.md",
            "wiki/Troubleshooting.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            guide_text = " ".join(guide.split())
            with self.subTest(guide=relative_path):
                self.assertIn("`v0.22.2` and current `main` still start", guide_text)
                self.assertIn("rejected at request time with `403", guide_text)
                self.assertNotIn("refuses to start while", guide_text)

    def test_segmented_history_tools_are_version_gated(self) -> None:
        for relative_path in (
            "wiki/Backup-Restore-and-Debug-Bundles.md",
            "wiki/History-and-Snapshot-Export.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            guide_text = " ".join(guide.split())
            with self.subTest(guide=relative_path):
                self.assertIn("packaged in images built from current `main`", guide_text)
                self.assertIn("not present in the published `v0.22.2` image", guide_text)

    def test_strict_host_key_guides_require_verified_preloading(self) -> None:
        for relative_path in (
            "wiki/SSH-Setup-and-Sudo.md",
            "wiki/Quantastor-Setup.md",
        ):
            guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            guide_text = " ".join(guide.split())
            with self.subTest(guide=relative_path):
                self.assertIn("current `main` uses Paramiko `RejectPolicy`", guide_text)
                self.assertIn("verify every host-key fingerprint", guide_text)
                self.assertIn("preload", guide_text)
                self.assertIn("`v0.22.2` instead uses trust on first use", guide_text)
                self.assertNotIn("first successful SSH connection pins", guide_text)

        ssh_guide = (REPO_ROOT / "wiki/SSH-Setup-and-Sudo.md").read_text(encoding="utf-8")
        self.assertIn("ssh-keyscan -T 5 -p 22 storage.example.test", ssh_guide)
        self.assertIn('ssh-keygen -lf "$known_hosts_candidate"', ssh_guide)
        self.assertIn("does not authenticate the key", ssh_guide)

    def test_scheduled_backup_archive_format_follows_scope(self) -> None:
        guide = (REPO_ROOT / "wiki/Backup-Restore-and-Debug-Bundles.md").read_text(
            encoding="utf-8"
        )
        guide_text = " ".join(guide.split())

        self.assertIn("Scopes that include `history_db` produce encrypted `.7z` archives", guide_text)
        self.assertIn(
            "Scopes without `history_db` produce encrypted `.tar.zst.enc` archives",
            guide_text,
        )
        self.assertIn("Both paths validate the finished archive", guide_text)
        self.assertNotIn("Legacy `.tar.zst.enc`", guide_text)
        self.assertNotIn("Both scheduled archive formats support schema 2", guide_text)

    def test_secret_overlay_grants_only_required_service_scoped_files(self) -> None:
        overlay_path = REPO_ROOT / "docker-compose.secrets.yml"
        self.assertTrue(overlay_path.is_file())
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        services = overlay["services"]
        self.assertEqual(set(services), {"enclosure-ui", "enclosure-admin"})
        self.assertEqual(set(overlay["secrets"]), set(EXPECTED_COMPOSE_SECRETS))

        ui_secret_names = set(services["enclosure-ui"]["secrets"])
        admin_secret_names = set(services["enclosure-admin"]["secrets"])
        self.assertEqual(ui_secret_names, set(EXPECTED_COMPOSE_SECRETS))
        self.assertEqual(admin_secret_names, set(EXPECTED_COMPOSE_SECRETS))

        for secret_name, env_name in EXPECTED_COMPOSE_SECRETS.items():
            with self.subTest(secret=secret_name):
                self.assertEqual(
                    services["enclosure-admin"]["environment"][env_name],
                    f"/run/secrets/{secret_name}",
                )
                source = overlay["secrets"][secret_name]["file"]
                self.assertTrue(str(source).startswith("./secrets/"))
        for secret_name in ui_secret_names:
            env_name = EXPECTED_COMPOSE_SECRETS[secret_name]
            self.assertEqual(
                services["enclosure-ui"]["environment"][env_name],
                f"/run/secrets/{secret_name}",
            )

        deployment_guide = (
            REPO_ROOT / "wiki/Docker-and-GHCR-Deployment.md"
        ).read_text(encoding="utf-8")
        self.assertIn("all five files into both UI and admin", deployment_guide)
        self.assertNotIn("only the four appliance/SSH files into the UI", deployment_guide)

        for compose_name in COMPOSE_FILES:
            base_compose = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))
            bind_roots: set[Path] = set()
            for service in base_compose["services"].values():
                for volume in service.get("volumes", []):
                    if isinstance(volume, str) and volume.startswith("./") and ":" in volume:
                        bind_roots.add((REPO_ROOT / volume.split(":", 1)[0]).resolve())
            for secret in overlay["secrets"].values():
                source_path = (REPO_ROOT / secret["file"]).resolve()
                for bind_root in bind_roots:
                    with self.subTest(
                        compose=compose_name,
                        source=source_path.name,
                        bind_root=bind_root.name,
                    ):
                        self.assertFalse(source_path == bind_root or bind_root in source_path.parents)

        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("secrets/*", gitignore.splitlines())

    def test_dockerfile_is_the_single_owner_of_the_ui_healthcheck(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        instructions: list[str] = []
        pending = ""
        for raw_line in dockerfile.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            pending = f"{pending} {stripped}".strip()
            if pending.endswith("\\"):
                pending = pending[:-1].rstrip()
                continue
            instructions.append(pending)
            pending = ""
        self.assertFalse(pending)
        healthchecks = [item for item in instructions if item.startswith("HEALTHCHECK ")]
        self.assertEqual(
            healthchecks,
            [
                "HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 "
                "CMD python -c \"import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=4)\""
            ],
        )

        for compose_name in COMPOSE_FILES:
            compose = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))
            services = compose["services"]
            with self.subTest(compose=compose_name, service="enclosure-ui"):
                self.assertNotIn("healthcheck", services["enclosure-ui"])
            for sidecar_name in ("enclosure-history", "enclosure-admin"):
                with self.subTest(compose=compose_name, service=sidecar_name):
                    self.assertIn("healthcheck", services[sidecar_name])
            with self.subTest(compose=compose_name, service="enclosure-backup"):
                self.assertEqual(services["enclosure-backup"].get("healthcheck"), {"disable": True})

    def test_all_compose_services_have_env_overridable_memory_limits(self) -> None:
        for compose_name in COMPOSE_FILES:
            compose = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))
            services = compose["services"]
            for service_name, expected_limit in EXPECTED_MEMORY_LIMITS.items():
                with self.subTest(compose=compose_name, service=service_name):
                    self.assertEqual(services[service_name].get("mem_limit"), expected_limit)

    def test_memory_limit_overrides_are_documented_in_env_example(self) -> None:
        example_lines = {
            line.strip()
            for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertIn("APP_MEM_LIMIT=1g", example_lines)
        self.assertIn("HISTORY_MEM_LIMIT=1g", example_lines)
        self.assertIn("ADMIN_MEM_LIMIT=3g", example_lines)
        self.assertIn("BACKUP_MEM_LIMIT=3g", example_lines)

    def test_history_permission_repair_is_explicit_and_documented(self) -> None:
        for compose_name in COMPOSE_FILES:
            compose = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))
            environment = compose["services"]["enclosure-history"]["environment"]
            with self.subTest(compose=compose_name):
                for env_name, expected_value in EXPECTED_HISTORY_PERMISSION_ENV.items():
                    self.assertEqual(environment.get(env_name), expected_value)

        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("HISTORY_PERMISSION_REPAIR_ENABLED=false", env_example)
        self.assertIn("HISTORY_SHARED_DIR_MODE=0770", env_example)
        self.assertIn("HISTORY_SHARED_FILE_MODE=0660", env_example)

        deployment_doc = (REPO_ROOT / "wiki" / "Docker-and-GHCR-Deployment.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("disabled by default", deployment_doc)
        self.assertIn("not world-writable", deployment_doc)
        self.assertIn("HISTORY_PERMISSION_REPAIR_ENABLED", deployment_doc)
        self.assertIn("roll back", deployment_doc)

    def test_scheduled_backup_service_is_one_shot_non_networked_and_has_no_docker_socket(self) -> None:
        for compose_name in COMPOSE_FILES:
            compose = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))
            service = compose["services"]["enclosure-backup"]
            with self.subTest(compose=compose_name):
                self.assertEqual(service["profiles"], ["backup"])
                self.assertEqual(
                    service["command"],
                    ["python", "-m", "history_service.scheduled_backup_main"],
                )
                self.assertEqual(service["network_mode"], "none")
                self.assertEqual(service["restart"], "no")
                self.assertEqual(service["user"], "${BACKUP_UID:-1000}:${BACKUP_GID:-1000}")
                self.assertNotIn("ports", service)
                self.assertFalse(
                    any("docker.sock" in volume for volume in service.get("volumes", []))
                )
                self.assertIn("./config:/app/config:ro", service["volumes"])
                self.assertIn("./data:/app/data:ro", service["volumes"])
                self.assertIn("./config/backup-secrets:/run/backup-secrets:ro", service["volumes"])
                environment = service["environment"]
                self.assertNotIn("SCHEDULED_BACKUP_PASSPHRASE", environment)
                self.assertIn("SCHEDULED_BACKUP_PASSPHRASE_FILE", environment)

                runbook = (
                    REPO_ROOT / "wiki/Backup-Restore-and-Debug-Bundles.md"
                ).read_text(encoding="utf-8")
                self.assertIn("BACKUP_UID=$(id -u)", runbook)
                self.assertIn("BACKUP_GID=$(id -g)", runbook)

            admin = compose["services"]["enclosure-admin"]
            with self.subTest(compose=compose_name, service="enclosure-admin"):
                self.assertEqual(admin["restart"], "no")
                self.assertEqual(
                    admin["environment"]["ADMIN_AUTO_STOP_SECONDS"],
                    "${ADMIN_AUTO_STOP_SECONDS:-3600}",
                )
                self.assertFalse(
                    any(key.startswith("ADMIN_SCHEDULED_BACKUP_") for key in admin["environment"])
                )

    def test_systemd_timer_invokes_one_shot_runner_without_secret_values(self) -> None:
        service = (REPO_ROOT / "deploy/systemd/truenas-jbod-system-backup.service").read_text(
            encoding="utf-8"
        )
        timer = (REPO_ROOT / "deploy/systemd/truenas-jbod-system-backup.timer").read_text(
            encoding="utf-8"
        )

        self.assertIn("docker compose --profile backup run --rm enclosure-backup", service)
        self.assertNotIn("passphrase=", service.lower())
        self.assertNotIn("Environment=SCHEDULED_BACKUP_PASSPHRASE", service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)

    def test_scheduled_backup_artifacts_status_and_secret_directory_are_git_ignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("config/backup-secrets/*", gitignore)
        self.assertIn("backups/*", gitignore)
        self.assertIn("backup-status/*", gitignore)

    def test_image_limits_glibc_malloc_arenas(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        logical_instructions: list[str] = []
        current = ""
        for raw_line in dockerfile.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            current = f"{current} {stripped}".strip()
            if current.endswith("\\"):
                current = current[:-1].rstrip()
                continue
            logical_instructions.append(current)
            current = ""
        if current:
            logical_instructions.append(current)

        env_instructions = [
            instruction
            for instruction in logical_instructions
            if instruction.startswith("ENV ")
        ]
        self.assertIn("MALLOC_ARENA_MAX=2", " ".join(env_instructions))


if __name__ == "__main__":
    unittest.main()
