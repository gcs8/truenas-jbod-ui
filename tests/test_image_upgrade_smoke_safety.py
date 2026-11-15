"""Root admission and cleanup safety of the image-upgrade smoke script (#1224, #1225).

Every privileged or Docker command is intercepted by a synthetic, stateful fake
at the command boundary. Nothing here starts Docker, runs ``sudo`` or touches a
real deployment.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[1]
COMPOSE_FIXTURE = REPOSITORY / "tests/fixtures/compose/v0.22.2.yml"
NONROOT_FIXTURE = REPOSITORY / "tests/fixtures/compose/v0.22.2.nonroot.yml"
CONFIG_FIXTURE = REPOSITORY / "tests/fixtures/ci-smoke-config.yaml"


def load_smoke():
    spec = importlib.util.spec_from_file_location(
        "run_image_upgrade_smoke_safety", REPOSITORY / "scripts" / "run_image_upgrade_smoke.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli_argv(root: Path, scenario: str = "base", compose: Path = COMPOSE_FIXTURE,
             config: Path = CONFIG_FIXTURE) -> list[str]:
    return ["--scenario", scenario, "--root", str(root),
            "--compose-fixture", str(compose), "--config-fixture", str(config),
            "--previous-image", "previous", "--previous-version", "1", "--candidate-image", "candidate",
            "--candidate-version", "2", "--candidate-revision", "revision"]


class StatefulPrivilegedFake:
    """Plays ``sudo install``/``sudo rm -rf`` against the temporary tree, records every command."""

    def __init__(self, smoke) -> None:
        self.smoke = smoke
        self.commands: list[list[str]] = []
        # CLI tests stop the scenario at its first recursive-deletion request
        # (as a failed command would) instead of letting it continue.
        self.stop_on_delete = False

    def run(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        if command[:2] == ["sudo", "rm"] and self.stop_on_delete:
            raise self.smoke.SmokeError("synthetic stop at a recursive deletion request")
        if command[:3] == ["sudo", "rm", "-rf"]:
            target = Path(command[-1])
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
        elif command[:3] == ["sudo", "install", "-d"]:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        elif command[:2] == ["sudo", "install"]:
            shutil.copyfile(command[-2], command[-1])
        return ""

    def subprocess_run(self, command, **kwargs):
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    def destructive(self) -> list[list[str]]:
        return [command for command in self.commands
                if command[:2] == ["sudo", "rm"] or command[:2] == ["docker", "compose"]]


class RootAdmissionTests(unittest.TestCase):
    """#1224: a --root path alone never grants recursive deletion authority."""

    def setUp(self) -> None:
        self.smoke = load_smoke()
        self.fake = StatefulPrivilegedFake(self.smoke)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name)
        patches = (mock.patch.object(self.smoke, "run", side_effect=self.fake.run),
                   mock.patch.object(self.smoke.subprocess, "run", side_effect=self.fake.subprocess_run))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def admission(self, root: Path, compose: Path = COMPOSE_FIXTURE, config: Path = CONFIG_FIXTURE,
                  nonroot: Path | None = None) -> str:
        """``admitted``, ``refused: <reason>``, or the name of another exception that escaped."""
        try:
            self.smoke.prepare_root(root, compose, config, nonroot)
        except self.smoke.SmokeError as exc:
            return f"refused: {exc}"
        except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
            return f"unhandled {type(exc).__name__}"
        return "admitted"

    def run_cli(self, argv: list[str]):
        """The CLI exit status, or the name of an exception it let escape."""
        self.fake.stop_on_delete = True
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            try:
                return self.smoke.main(argv)
            except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
                return f"unhandled {type(exc).__name__}"

    def operator_tree(self, name: str = "operator-install") -> tuple[Path, Path]:
        root = self.parent / name
        root.mkdir()
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        marker = root / "operator-kept.txt"
        marker.write_text("synthetic operator data\n", encoding="utf-8")
        return root, marker

    def test_existing_nonempty_root_is_refused_before_any_command(self):
        root, marker = self.operator_tree()
        self.assertRegex(self.admission(root), "^refused: .*already exists")
        self.assertEqual(self.fake.commands, [])
        self.assertTrue(marker.is_file())

    def test_existing_empty_root_is_refused_before_any_command(self):
        root = self.parent / "empty"
        root.mkdir()
        self.assertRegex(self.admission(root), "^refused: .*already exists")
        self.assertEqual(self.fake.commands, [])
        self.assertEqual(list(root.iterdir()), [])

    def test_symlinked_root_is_refused_and_its_target_kept(self):
        target, marker = self.operator_tree()
        for link_target in (target, self.parent / "absent-target"):
            with self.subTest(link_target=link_target.name):
                link = self.parent / f"link-to-{link_target.name}"
                link.symlink_to(link_target, target_is_directory=True)
                self.assertRegex(self.admission(link), "^refused: .*symlink")
                self.assertEqual(self.fake.commands, [])
                self.assertTrue(link.is_symlink())
                self.assertFalse((self.parent / "absent-target").exists())
        self.assertTrue(marker.is_file())

    def test_missing_fixture_is_refused_before_the_root_is_created(self):
        root = self.parent / "smoke-root"
        missing = self.parent / "missing.yml"
        for compose, config, nonroot in ((missing, CONFIG_FIXTURE, None),
                                         (COMPOSE_FIXTURE, missing, None),
                                         (COMPOSE_FIXTURE, CONFIG_FIXTURE, missing),
                                         (self.parent, CONFIG_FIXTURE, None)):
            with self.subTest(compose=compose.name, config=config.name, nonroot=nonroot):
                self.assertRegex(self.admission(root, compose, config, nonroot), "^refused: .*fixture")
                self.assertEqual(self.fake.commands, [])
                self.assertFalse(root.exists())

    def test_cli_with_an_existing_root_runs_no_destructive_command(self):
        root, marker = self.operator_tree()
        for scenario in ("base", "readonly-state", "admin-during-upgrade"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.run_cli(cli_argv(root, scenario)), 1)
            self.assertEqual(self.fake.destructive(), [])
            self.assertTrue(marker.is_file())

    def test_cli_does_not_resolve_a_symlinked_root_into_its_target(self):
        target, marker = self.operator_tree()
        link = self.parent / "smoke-root"
        link.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_cli(cli_argv(link)), 1)
        self.assertEqual(self.fake.destructive(), [])
        self.assertTrue(link.is_symlink())
        self.assertTrue(marker.is_file())

    def test_cli_with_a_missing_fixture_creates_and_deletes_nothing(self):
        root = self.parent / "smoke-root"
        self.assertEqual(self.run_cli(cli_argv(root, compose=self.parent / "missing.yml")), 1)
        self.assertEqual(self.fake.commands, [])
        self.assertFalse(root.exists())

    def test_admitted_new_root_is_created_and_owned_by_this_run(self):
        # Passing control: an absent child of an existing directory is created
        # by this invocation, populated, and only then may be replaced again.
        root = self.parent / "smoke-root"
        self.assertEqual(self.admission(root, nonroot=NONROOT_FIXTURE), "admitted")
        self.assertTrue((root / "docker-compose.yml").is_file())
        self.assertTrue((root / "docker-compose.nonroot.yml").is_file())
        self.assertTrue((root / "config" / "config.yaml").is_file())
        self.assertNotIn(["sudo", "rm", "-rf", str(root)], self.fake.commands)
        self.assertNotIn(["sudo", "rm", "-rf", "--", str(root)], self.fake.commands)
        self.assertTrue(self.smoke.owns_root(root))
        # The admin scenario rebuilds its own root; that is still this run's generation.
        (root / "left-by-phase-one").write_text("synthetic\n", encoding="utf-8")
        self.assertEqual(self.admission(root), "admitted")
        self.assertFalse((root / "left-by-phase-one").exists())
        self.assertTrue((root / "config" / "config.yaml").is_file())
        self.assertFalse(self.smoke.owns_root(self.parent / "other"))

    def test_scenarios_do_not_resolve_the_supplied_root(self):
        import inspect
        for name in ("upgrade_and_rollback", "segmented_catalog_upgrade", "interrupted_migration",
                     "readonly_state", "enospc_state", "unavailable_backup_storage", "admin_during_upgrade",
                     "main"):
            with self.subTest(function=name):
                source = inspect.getsource(getattr(self.smoke, name))
                self.assertFalse("root.resolve()" in source, f"{name} resolves --root before admission")


if __name__ == "__main__":
    unittest.main()
