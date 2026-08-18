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
