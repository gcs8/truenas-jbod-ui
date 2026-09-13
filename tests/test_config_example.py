from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml
from pydantic import BaseModel

from app.config import Settings, SystemConfig

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "config" / "config.example.yaml"

# Settings fields the example deliberately leaves out, with the reason.
ALLOWED_MISSING = {
    # Derived from APP_CONFIG_PATH; never read from the file itself.
    "config_file",
    # Enclosure profiles are documented in config/profiles.example.yaml.
    "profiles",
    # Read by nothing; the field is removed from the model separately.
    "app.verify_ssl",
}


def _is_allowed_missing(path: str) -> bool:
    return path in ALLOWED_MISSING or any(path.startswith(f"{allowed}.") for allowed in ALLOWED_MISSING)


def _model_paths(model: type[BaseModel], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        paths.add(path)
        annotation = field.annotation
        candidates = [annotation, *(getattr(annotation, "__args__", None) or ())]
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                paths |= _model_paths(candidate, f"{path}.")
    return paths


class ConfigExampleCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = EXAMPLE_PATH.read_text(encoding="utf-8")
        self.active = yaml.safe_load(self.text)

    def _commented_systems(self) -> dict[str, dict]:
        start = self.text.index("# default_system_id:")
        end = self.text.index("\nlayout:")
        region = self.text[start:end].splitlines()
        self.assertTrue(all(line.startswith("#") for line in region))
        block = yaml.safe_load("\n".join(line[1:] for line in region))
        return {system["id"]: system for system in block["systems"]}

    def test_active_sections_name_every_option_with_its_default(self) -> None:
        settings = Settings.model_validate(self.active)
        defaults = Settings()
        for section in ("app", "perf", "truenas", "ssh", "history", "admin", "layout", "paths"):
            with self.subTest(section=section):
                model = type(getattr(defaults, section))
                expected = {
                    name for name in model.model_fields if not _is_allowed_missing(f"{section}.{name}")
                }
                self.assertEqual(set(self.active[section]), expected)
        self.assertNotIn("verify_ssl", self.active["app"])
        self.assertEqual(settings.app.model_dump(exclude={"verify_ssl"}), defaults.app.model_dump(exclude={"verify_ssl"}))
        self.assertEqual(settings.perf, defaults.perf)
        self.assertEqual(settings.layout, defaults.layout)
        self.assertFalse(settings.truenas.verify_ssl)

    def test_every_settings_field_appears_in_the_example(self) -> None:
        keys = set(re.findall(r"(?m)^#?\s*(?:-\s+)?([A-Za-z_][A-Za-z0-9_]*):", self.text))
        missing = sorted(
            path
            for path in _model_paths(Settings)
            if not _is_allowed_missing(path) and path.rsplit(".", 1)[-1] not in keys
        )
        self.assertEqual(missing, [])

    def test_systems_examples_cover_every_platform_and_the_system_fields(self) -> None:
        systems = self._commented_systems()
        for system_id, system in systems.items():
            with self.subTest(system=system_id):
                SystemConfig.model_validate(system)

        platforms = {system["truenas"]["platform"] for system in systems.values()}
        self.assertEqual(platforms, {"core", "scale", "linux", "quantastor", "esxi", "ipmi"})
        self.assertTrue(any(system.get("bmc", {}).get("enabled") for system in systems.values()))
        self.assertTrue(any(system.get("storage_views") for system in systems.values()))
        self.assertTrue(any(system.get("ssh", {}).get("ha_nodes") for system in systems.values()))
        # The beginner path never demonstrates verified TLS in a system block.
        self.assertFalse(any(system["truenas"].get("verify_ssl") for system in systems.values()))

    def test_example_uses_documentation_hosts_and_no_stale_version_wording(self) -> None:
        self.assertNotIn("v0.2+", self.text)
        hosts = re.findall(r"(?m)^#?\s*(?:-\s+)?host:\s*(\S+)", self.text)
        self.assertGreater(len(hosts), 10)
        for host in hosts:
            with self.subTest(host=host):
                # app.host is the listen address, not a documentation host.
                self.assertTrue(
                    host == "0.0.0.0" or host.endswith(".example.test") or host.startswith("192.0.2."),
                    host,
                )


if __name__ == "__main__":
    unittest.main()
