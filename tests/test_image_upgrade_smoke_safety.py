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


def claim(smoke, root: Path) -> None:
    """Record ``root`` as created by this run, as prepare_root does (no-op where ownership is untracked)."""
    owned = getattr(smoke, "_OWNED_ROOTS", None)
    if owned is not None:
        owned.add(root)


def expected_cleanup_commands(root: Path, chain: tuple[str, ...] = (), *, unmount: bool = False) -> list[list[str]]:
    """The verified cleanup sequence for an owned root with the given ``-f`` chain."""
    files = [arg for name in chain for arg in ("-f", name)]
    project = f"label=com.docker.compose.project={root.name.lower()}"
    return [
        ["docker", "compose", *files, "--profile", "history", "--profile", "admin",
         "down", "--volumes", "--remove-orphans"],
        *([["sudo", "umount", str(root / "history")]] if unmount else []),
        ["docker", "container", "ls", "--all", "--quiet", "--filter", project],
        ["docker", "network", "ls", "--quiet", "--filter", project],
        ["docker", "volume", "ls", "--quiet", "--filter", project],
        ["sudo", "rm", "-rf", "--", str(root)],
    ]


class CleanupBoundary:
    """Stateful fake for every command cleanup may run; nothing reaches Docker or sudo."""

    def __init__(self, root: Path, *, down=0, census=None, umount=0, remove=0) -> None:
        self.root = root
        self.down = down          # exit status, or an exception instance to raise
        self.census = census or {}  # kind -> stdout, exit status, or exception
        self.umount = umount
        self.remove = remove
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.stdout_at_removal: str | None = None
        self.stdout = None

    def __call__(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        self.kwargs.append(kwargs)
        if command[:2] == ["docker", "compose"] and "down" in command:
            outcome = self.down
        elif command[:1] == ["docker"] and "ls" in command:
            outcome = self.census.get(command[1], "")
        elif command[:2] == ["sudo", "umount"]:
            outcome = self.umount
        elif command[:3] == ["sudo", "rm", "-rf"]:
            if self.stdout is not None:
                self.stdout_at_removal = self.stdout.getvalue()
            outcome = self.remove
            if outcome == 0:
                shutil.rmtree(self.root)
        else:
            outcome = 0
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, str):
            return subprocess.CompletedProcess(command, 0, outcome, "")
        return subprocess.CompletedProcess(command, outcome, "", "")

    def removal_requested(self) -> bool:
        return any(command[:3] == ["sudo", "rm", "-rf"] for command in self.commands)

    def census_requested(self) -> list[str]:
        return [command[1] for command in self.commands if command[:1] == ["docker"] and "ls" in command]


class CleanupVerificationTests(unittest.TestCase):
    """#1225: success needs verified cleanup; failed or unknown cleanup retains the root."""

    def setUp(self) -> None:
        self.smoke = load_smoke()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "image-upgrade-smoke"
        self.recovery = self.root / "config" / "config.yaml"
        self.ensure_root()

    def ensure_root(self) -> None:
        """A populated owned root for each case, even after an earlier subtest removed it."""
        self.recovery.parent.mkdir(parents=True, exist_ok=True)
        (self.root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.recovery.write_text("synthetic recovery configuration\n", encoding="utf-8")
        claim(self.smoke, self.root)

    def cleanup_outcome(self, boundary: CleanupBoundary) -> str:
        """``verified``, ``refused: <reason>``, or ``returned <value>`` when no failure was reported."""
        self.ensure_root()
        with mock.patch.object(self.smoke.subprocess, "run", side_effect=boundary), \
             mock.patch.object(self.smoke.os.path, "ismount", return_value=False):
            try:
                returned = self.smoke.cleanup(self.root)
            except self.smoke.SmokeError as exc:
                return f"refused: {exc}"
            except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
                return f"unhandled {type(exc).__name__}"
        if not self.root.exists():
            return "verified"
        return f"returned {returned!r} with the root still present"

    def test_verified_cleanup_removes_the_root(self):
        boundary = CleanupBoundary(self.root)
        self.assertEqual(self.cleanup_outcome(boundary), "verified")
        self.assertEqual(boundary.commands, expected_cleanup_commands(self.root))

    def test_every_cleanup_command_has_a_deadline(self):
        boundary = CleanupBoundary(self.root)
        self.cleanup_outcome(boundary)
        self.assertTrue(boundary.kwargs)
        for command, kwargs in zip(boundary.commands, boundary.kwargs):
            with self.subTest(command=command[:3]):
                self.assertIsInstance(kwargs.get("timeout"), (int, float))

    def test_failed_teardown_retains_the_root_and_still_reads_back(self):
        boundary = CleanupBoundary(self.root, down=1)
        self.assertRegex(self.cleanup_outcome(boundary), "^refused: .*teardown failed")
        self.assertFalse(boundary.removal_requested())
        self.assertEqual(boundary.census_requested(), ["container", "network", "volume"])
        self.assertTrue(self.recovery.is_file())

    def test_timed_out_teardown_retains_the_root(self):
        boundary = CleanupBoundary(self.root, down=subprocess.TimeoutExpired(["docker"], 1))
        self.assertRegex(self.cleanup_outcome(boundary), "^refused: .*teardown failed")
        self.assertFalse(boundary.removal_requested())
        self.assertTrue(self.recovery.is_file())

    def test_residual_resources_after_teardown_retain_the_root(self):
        for kind in ("container", "network", "volume"):
            with self.subTest(kind=kind):
                boundary = CleanupBoundary(self.root, census={kind: "0123456789ab\n"})
                self.assertRegex(self.cleanup_outcome(boundary), "^refused: .*remain")
                self.assertFalse(boundary.removal_requested())
                self.assertTrue(self.recovery.is_file())

    def test_unknown_resource_census_retains_the_root(self):
        for kind, outcome in (("container", 1), ("network", subprocess.TimeoutExpired(["docker"], 1)),
                              ("volume", OSError("synthetic"))):
            with self.subTest(kind=kind):
                boundary = CleanupBoundary(self.root, census={kind: outcome})
                self.assertRegex(self.cleanup_outcome(boundary), "^refused: .*unknown")
                self.assertFalse(boundary.removal_requested())
                self.assertTrue(self.recovery.is_file())

    def test_failed_removal_is_reported(self):
        for outcome in (1, subprocess.TimeoutExpired(["sudo"], 1)):
            with self.subTest(outcome=outcome):
                boundary = CleanupBoundary(self.root, remove=outcome)
                self.assertRegex(self.cleanup_outcome(boundary), "^refused: .*removal")

    def test_unowned_root_is_never_cleaned(self):
        stranger = self.root.parent / "not-created-by-this-run"
        stranger.mkdir()
        (stranger / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        boundary = CleanupBoundary(stranger)
        with mock.patch.object(self.smoke.subprocess, "run", side_effect=boundary):
            with self.assertRaisesRegex(self.smoke.SmokeError, "not created by this run"):
                self.smoke.cleanup(stranger)
        self.assertEqual(boundary.commands, [])
        self.assertTrue(stranger.is_dir())

    def run_cli(self, boundary: CleanupBoundary, failure: Exception | None) -> tuple[object, str, str]:
        import io
        self.ensure_root()
        stdout, stderr = io.StringIO(), io.StringIO()
        boundary.stdout = stdout
        with mock.patch.object(self.smoke, "smoke", side_effect=failure,
                               return_value=f"{self.smoke.SUCCESS_PREFIX} scenario=base synthetic"), \
             mock.patch.object(self.smoke.subprocess, "run", side_effect=boundary), \
             mock.patch.object(self.smoke.os.path, "ismount", return_value=False), \
             mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            try:
                status = self.smoke.main(cli_argv(self.root))
            except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
                status = f"unhandled {type(exc).__name__}"
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_success_waits_for_verified_cleanup(self):
        boundary = CleanupBoundary(self.root)
        status, stdout, _ = self.run_cli(boundary, None)
        self.assertEqual(status, 0)
        self.assertIn(self.smoke.SUCCESS_PREFIX, stdout)
        self.assertEqual(boundary.stdout_at_removal, "", "success was printed before cleanup was verified")
        self.assertFalse(self.root.exists())

    def test_cli_successful_scenario_with_failed_cleanup_fails(self):
        for outcome in ({"down": 1}, {"census": {"container": "0123456789ab\n"}}, {"remove": 1}):
            with self.subTest(outcome=outcome):
                boundary = CleanupBoundary(self.root, **outcome)
                status, stdout, stderr = self.run_cli(boundary, None)
                self.assertEqual(status, 1)
                self.assertNotIn(self.smoke.SUCCESS_PREFIX, stdout)
                self.assertIn("cleanup", stderr)
                self.assertTrue(self.root.is_dir())

    def test_cli_failed_cleanup_does_not_hide_the_scenario_failure(self):
        boundary = CleanupBoundary(self.root, down=1)
        status, stdout, stderr = self.run_cli(boundary, self.smoke.SmokeError("synthetic scenario refusal"))
        self.assertEqual(status, 1)
        self.assertNotIn(self.smoke.SUCCESS_PREFIX, stdout)
        self.assertIn("synthetic scenario refusal", stderr)
        self.assertRegex(stderr, "cleanup.*teardown failed")
        self.assertLess(stderr.index("synthetic scenario refusal"), stderr.index("teardown failed"))
        self.assertTrue(self.recovery.is_file())


if __name__ == "__main__":
    unittest.main()
