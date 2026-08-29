from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dev.yml")
EXPECTED_MEMORY_LIMITS = {
    "enclosure-ui": "${APP_MEM_LIMIT:-1g}",
    "enclosure-history": "${HISTORY_MEM_LIMIT:-1g}",
    "enclosure-admin": "${ADMIN_MEM_LIMIT:-1g}",
    "enclosure-backup": "${BACKUP_MEM_LIMIT:-1g}",
}


class ContainerResourceContractTests(unittest.TestCase):
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
