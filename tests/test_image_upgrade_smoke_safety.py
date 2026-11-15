"""Root admission and cleanup safety of the image-upgrade smoke script (#1224, #1225).

Every privileged or Docker command is intercepted by a synthetic, stateful fake
at the command boundary. Nothing here starts Docker, runs ``sudo`` or touches a
real deployment.
"""
from __future__ import annotations

import importlib.util
import os
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
        # prepare_root never removes anything, not even this run's own root: a
        # rebuild (the admin scenario's second phase) goes through the checked cleanup.
        (root / "left-by-phase-one").write_text("synthetic\n", encoding="utf-8")
        self.assertRegex(self.admission(root), "^refused: .*already exists")
        self.assertTrue((root / "left-by-phase-one").is_file())
        self.assertFalse(any(command[:2] == ["sudo", "rm"] for command in self.fake.commands))
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
    """Record the directory now at ``root`` as created by this run, exactly as prepare_root does."""
    smoke._record_created_root(root)


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


class ComposeHostFake:
    """``subprocess.run`` stand-in: plays ``sudo`` on the temporary tree and a Compose host by project.

    The effective project follows Compose's own precedence: ``--project-name``,
    then ``COMPOSE_PROJECT_NAME`` from the environment, then the working
    directory's name. ``up`` creates a container in that project and ``down``
    removes the project's containers unless ``leave_on_down`` keeps them (a
    teardown that exits 0 but leaves something behind).
    """

    def __init__(self, *, leave_on_down: bool = False, fail_compose: bool = False, on_down=None) -> None:
        self.leave_on_down = leave_on_down
        self.fail_compose = fail_compose
        self.on_down = on_down
        self.containers: dict[str, set[str]] = {}
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []

    @staticmethod
    def effective_project(command: list[str], kwargs: dict) -> str:
        if "--project-name" in command:
            return command[command.index("--project-name") + 1]
        env = kwargs.get("env")
        env = os.environ if env is None else env
        if env.get("COMPOSE_PROJECT_NAME"):
            return env["COMPOSE_PROJECT_NAME"]
        return Path(kwargs.get("cwd") or os.getcwd()).name.lower()

    def __call__(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        self.kwargs.append(kwargs)
        stdout = ""
        status = 0
        if command[:3] == ["sudo", "install", "-d"]:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        elif command[:2] == ["sudo", "install"]:
            shutil.copyfile(command[-2], command[-1])
        elif command[:3] == ["sudo", "rm", "-rf"]:
            target = Path(command[-1])
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
        elif command[:2] == ["docker", "compose"]:
            project = self.effective_project(command, kwargs)
            if self.fail_compose:
                status = 1
            elif "up" in command:
                self.containers.setdefault(project, set()).add(f"{project}-enclosure-ui-1")
            elif "down" in command:
                if self.on_down is not None:
                    self.on_down()
                if not self.leave_on_down:
                    self.containers.pop(project, None)
        elif command[:1] == ["docker"] and "ls" in command:
            label = next(arg for arg in command if arg.startswith("label=com.docker.compose.project="))
            project = label.split("=", 2)[2]
            if command[1] == "container":
                stdout = "".join(f"{name}\n" for name in sorted(self.containers.get(project, ())))
        if not kwargs.get("text"):
            stdout = stdout.encode()
        return subprocess.CompletedProcess(command, status, stdout, "" if kwargs.get("text") else b"")

    def removal_requested(self) -> bool:
        return any(command[:3] == ["sudo", "rm", "-rf"] for command in self.commands)

    def compose_commands(self) -> list[list[str]]:
        return [command for command in self.commands if command[:2] == ["docker", "compose"]]

    def census_projects(self) -> list[str]:
        return [arg.split("=", 2)[2] for command in self.commands if command[:1] == ["docker"] and "ls" in command
                for arg in command if arg.startswith("label=com.docker.compose.project=")]


class RoundTwoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.smoke = load_smoke()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(os.path.realpath(directory.name))

    def prepare(self, root: Path, host: ComposeHostFake) -> None:
        with mock.patch.object(self.smoke.subprocess, "run", side_effect=host):
            self.smoke.prepare_root(root, COMPOSE_FIXTURE, CONFIG_FIXTURE)
        host.commands.clear()
        host.kwargs.clear()

    def cleanup_outcome(self, root: Path, host: ComposeHostFake) -> str:
        with mock.patch.object(self.smoke.subprocess, "run", side_effect=host):
            try:
                self.smoke.cleanup(root)
            except self.smoke.SmokeError as exc:
                return f"refused: {exc}"
        return "verified"

    def run_cli(self, argv: list[str], host: ComposeHostFake) -> tuple[object, str]:
        import io
        stderr = io.StringIO()
        with mock.patch.object(self.smoke.subprocess, "run", side_effect=host), \
             mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", stderr):
            try:
                status = self.smoke.main(argv)
            except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
                status = f"unhandled {type(exc).__name__}"
        return status, stderr.getvalue()


class SingleRealRootTests(RoundTwoCase):
    """Admission and execution address one directory: the real path admitted before any command."""

    def test_parent_traversal_after_a_symlink_is_refused_before_any_command(self):
        (self.parent / "outside" / "inner").mkdir(parents=True)
        (self.parent / "link").symlink_to(self.parent / "outside" / "inner", target_is_directory=True)
        operator = self.parent / "outside" / "operator-install"
        operator.mkdir()
        (operator / ".env").write_text("synthetic-operator-env\n", encoding="utf-8")
        supplied = self.parent / "link" / ".." / "operator-install"
        host = ComposeHostFake(fail_compose=True)
        status, stderr = self.run_cli(cli_argv(supplied), host)
        self.assertEqual(status, 1)
        self.assertIn("'..'", stderr)
        self.assertEqual(host.commands, [])
        self.assertFalse((self.parent / "operator-install").exists(), "a different, lexical root was created")
        self.assertEqual((operator / ".env").read_text(encoding="utf-8"), "synthetic-operator-env\n")
        self.assertEqual(sorted(path.name for path in operator.iterdir()), [".env"])

    def test_relative_parent_traversal_is_refused_too(self):
        (self.parent / "work").mkdir()
        original = os.getcwd()
        os.chdir(self.parent / "work")
        self.addCleanup(os.chdir, original)
        with mock.patch.object(self.smoke, "run") as run:
            with self.assertRaisesRegex(self.smoke.SmokeError, "'\\.\\.'"):
                self.smoke.prepare_root(Path("..") / "smoke-root", COMPOSE_FIXTURE, CONFIG_FIXTURE)
        run.assert_not_called()
        self.assertFalse((self.parent / "smoke-root").exists())

    def test_symlinked_ancestor_without_traversal_runs_on_its_one_real_path(self):
        # Passing control: an ancestor symlink is resolved once, before any
        # command, and every later command uses that same real directory.
        real = self.parent / "real"
        real.mkdir()
        (self.parent / "alias").symlink_to(real, target_is_directory=True)
        host = ComposeHostFake(fail_compose=True)
        status, _ = self.run_cli(cli_argv(self.parent / "alias" / "smoke-root"), host)
        self.assertEqual(status, 1)  # the synthetic Compose host refuses to start anything
        resolved = real / "smoke-root"
        self.assertTrue(resolved.is_dir(), "failed teardown keeps the created root")
        self.assertTrue(host.compose_commands())
        for command, kwargs in zip(host.commands, host.kwargs):
            if command[:2] == ["docker", "compose"]:
                self.assertEqual(kwargs.get("cwd"), resolved)
            elif command[:2] == ["sudo", "install"]:
                self.assertTrue(command[-1].startswith(str(resolved) + os.sep), command)
        self.assertFalse(host.removal_requested())


class CreatedGenerationOwnershipTests(RoundTwoCase):
    """Deletion authority belongs to the directory this run created, not to its pathname."""

    def test_replaced_root_directory_is_refused_before_any_command(self):
        root = self.parent / "smoke-root"
        host = ComposeHostFake()
        self.prepare(root, host)
        root.rename(self.parent / "owned-original")
        root.mkdir()
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        marker = root / "operator-kept.txt"
        marker.write_text("synthetic replacement data\n", encoding="utf-8")
        self.assertRegex(self.cleanup_outcome(root, host), "^refused: .*no longer the directory this run created")
        self.assertEqual(host.commands, [])
        self.assertTrue(marker.is_file())
        self.assertFalse(self.smoke.owns_root(root))

    def test_retargeted_parent_symlink_does_not_inherit_deletion_authority(self):
        owned_parent = self.parent / "owned-parent"
        owned_parent.mkdir()
        foreign = self.parent / "foreign-parent" / "smoke-root"
        foreign.mkdir(parents=True)
        (foreign / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        marker = foreign / "operator-kept.txt"
        marker.write_text("synthetic foreign data\n", encoding="utf-8")
        link = self.parent / "link"
        link.symlink_to(owned_parent, target_is_directory=True)
        host = ComposeHostFake()
        # Admitted the way the scenarios admit --root before prepare_root.
        admit = getattr(self.smoke, "admitted_root", Path.absolute)
        self.prepare(admit(link / "smoke-root"), host)
        link.unlink()
        link.symlink_to(self.parent / "foreign-parent", target_is_directory=True)
        self.assertRegex(self.cleanup_outcome(link / "smoke-root", host), "^refused: .*not created by this run")
        self.assertEqual(host.commands, [])
        self.assertTrue(marker.is_file())
        # Control: the directory this run really created is still its own to clean up.
        self.assertEqual(self.cleanup_outcome(owned_parent / "smoke-root", host), "verified")
        self.assertFalse((owned_parent / "smoke-root").exists())
        self.assertTrue(marker.is_file())

    def test_ancestor_swapped_for_a_symlink_is_refused(self):
        holder = self.parent / "holder"
        holder.mkdir()
        root = holder / "smoke-root"
        host = ComposeHostFake()
        self.prepare(root, host)
        # The same directory is still reachable, but through a symlink the run
        # never admitted: the chain changed, so nothing is removed.
        holder.rename(self.parent / "holder-moved")
        holder.symlink_to(self.parent / "holder-moved", target_is_directory=True)
        self.assertRegex(self.cleanup_outcome(root, host), "^refused: ")
        self.assertFalse(host.removal_requested())
        self.assertEqual(host.compose_commands(), [])
        self.assertTrue((self.parent / "holder-moved" / "smoke-root" / "config" / "config.yaml").is_file())

    def test_root_replaced_during_teardown_is_checked_again_before_removal(self):
        root = self.parent / "smoke-root"
        replacement_marker = root / "operator-kept.txt"

        def swap() -> None:
            root.rename(self.parent / "owned-original")
            root.mkdir()
            replacement_marker.write_text("synthetic replacement data\n", encoding="utf-8")

        host = ComposeHostFake(on_down=swap)
        self.prepare(root, host)
        self.assertRegex(self.cleanup_outcome(root, host), "^refused: .*no longer the directory this run created")
        self.assertFalse(host.removal_requested())
        self.assertTrue(replacement_marker.is_file())
        self.assertTrue((self.parent / "owned-original" / "config" / "config.yaml").is_file())

    def test_unchanged_created_root_is_cleaned_up(self):
        # Passing control for the three refusals above.
        root = self.parent / "smoke-root"
        host = ComposeHostFake()
        self.prepare(root, host)
        self.assertTrue(self.smoke.owns_root(root))
        self.assertEqual(self.cleanup_outcome(root, host), "verified")
        self.assertFalse(root.exists())
        self.assertFalse(self.smoke.owns_root(root))


class AdminPhaseRebuildTests(RoundTwoCase):
    """The admin scenario's phase-one root is removed only through the checked cleanup."""

    def exercise(self, host: ComposeHostFake) -> tuple[str, Path, list]:
        root = self.parent / "admin-root"
        args = self.smoke.build_parser().parse_args(cli_argv(root, scenario="admin-during-upgrade"))
        seeds = []

        def seed(deployment, _args):
            seeds.append(deployment)
            if len(seeds) == 1:
                (root / "phase-one-recovery").write_text("synthetic phase one\n", encoding="utf-8")
                return None
            raise self.smoke.SmokeError("synthetic stop after the phase-two rebuild")

        with mock.patch.object(self.smoke.subprocess, "run", side_effect=host), \
             mock.patch.object(self.smoke, "seed_previous_release", side_effect=seed), \
             mock.patch.object(self.smoke, "check_admin_dormant"), \
             mock.patch.object(self.smoke, "check_runtime"), \
             mock.patch.object(self.smoke.Deployment, "pull_and_up"):
            try:
                self.smoke.admin_during_upgrade(args)
                outcome = "completed"
            except self.smoke.SmokeError as exc:
                outcome = str(exc)
        return outcome, root, seeds

    def test_phase_one_resources_left_after_teardown_keep_its_files(self):
        host = ComposeHostFake(leave_on_down=True)
        host.containers["admin-root"] = {"admin-root-enclosure-ui-1"}
        outcome, root, seeds = self.exercise(host)
        self.assertRegex(outcome, "containers remain")
        self.assertEqual(len(seeds), 1, "phase two must not start over unverified phase-one resources")
        self.assertFalse(host.removal_requested())
        self.assertTrue((root / "phase-one-recovery").is_file())

    def test_phase_one_root_is_removed_only_after_the_readback(self):
        # Passing control: verified teardown, then a fresh phase-two root.
        host = ComposeHostFake()
        outcome, root, seeds = self.exercise(host)
        self.assertEqual(outcome, "synthetic stop after the phase-two rebuild")
        self.assertEqual(len(seeds), 2)
        removal = next(index for index, command in enumerate(host.commands) if command[:3] == ["sudo", "rm", "-rf"])
        census = [index for index, command in enumerate(host.commands) if command[:1] == ["docker"] and "ls" in command]
        self.assertEqual(len(census), 3)
        self.assertLess(max(census), removal)
        self.assertFalse((root / "phase-one-recovery").exists())
        self.assertTrue((root / "config" / "config.yaml").is_file())

if __name__ == "__main__":
    unittest.main()
