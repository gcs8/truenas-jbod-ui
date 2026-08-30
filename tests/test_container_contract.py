from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dev.yml")
SUPPORTED_COMPOSE_FILES = (*COMPOSE_FILES, "docker-compose.secrets.yml")
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
    "enclosure-admin": "${ADMIN_MEM_LIMIT:-1g}",
    "enclosure-backup": "${BACKUP_MEM_LIMIT:-1g}",
}
EXPECTED_HISTORY_PERMISSION_ENV = {
    "HISTORY_PERMISSION_REPAIR_ENABLED": "${HISTORY_PERMISSION_REPAIR_ENABLED:-false}",
    "HISTORY_SHARED_DIR_MODE": "${HISTORY_SHARED_DIR_MODE:-0770}",
    "HISTORY_SHARED_FILE_MODE": "${HISTORY_SHARED_FILE_MODE:-0660}",
}


class ContainerResourceContractTests(unittest.TestCase):
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

    def test_secret_overlay_grants_only_required_service_scoped_files(self) -> None:
        overlay_path = REPO_ROOT / "docker-compose.secrets.yml"
        self.assertTrue(overlay_path.is_file())
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        services = overlay["services"]
        self.assertEqual(set(services), {"enclosure-ui", "enclosure-admin"})
        self.assertEqual(set(overlay["secrets"]), set(EXPECTED_COMPOSE_SECRETS))

        ui_secret_names = set(services["enclosure-ui"]["secrets"])
        admin_secret_names = set(services["enclosure-admin"]["secrets"])
        self.assertEqual(ui_secret_names, set(EXPECTED_COMPOSE_SECRETS) - {"admin_auth_password"})
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
        self.assertIn("ADMIN_MEM_LIMIT=1g", example_lines)
        self.assertIn("BACKUP_MEM_LIMIT=1g", example_lines)

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
