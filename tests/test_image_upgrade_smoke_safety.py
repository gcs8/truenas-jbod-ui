"""Root admission and cleanup safety of the image-upgrade smoke script (#1224, #1225).

Every privileged or Docker command is intercepted by a synthetic, stateful fake
at the command boundary. Nothing here starts Docker, runs ``sudo`` or touches a
real deployment.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[1]
COMPOSE_FIXTURE = REPOSITORY / "tests/fixtures/compose/v0.22.2.yml"
NONROOT_FIXTURE = REPOSITORY / "tests/fixtures/compose/v0.22.2.nonroot.yml"
CONFIG_FIXTURE = REPOSITORY / "tests/fixtures/ci-smoke-config.yaml"


_PREVIOUS_UMASK: list[int] = []


def setUpModule() -> None:
    # The smoke refuses a root that another user could rename entries in,
    # group-writable included; build the synthetic trees with the usual umask
    # whatever the runner's is (the script itself creates its roots 0700).
    _PREVIOUS_UMASK.append(os.umask(0o022))


def tearDownModule() -> None:
    if _PREVIOUS_UMASK:
        os.umask(_PREVIOUS_UMASK.pop())


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
    """Record the directory now at ``root`` as created by this run, exactly as prepare_root does (mode 0700)."""
    os.chmod(root, 0o700)
    smoke._record_created_root(root)


def expected_cleanup_commands(root: Path, chain: tuple[str, ...] = (), *, unmount: bool = False) -> list[list[str]]:
    """The verified cleanup sequence for an owned root with the given ``-f`` chain."""
    files = [arg for name in chain for arg in ("-f", name)]
    project = f"label=com.docker.compose.project={root.name.lower()}"
    return [
        ["docker", "compose", "--project-name", root.name.lower(), *files, "--profile", "history", "--profile", "admin",
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
        # The ENOSPC overlay makes cleanup unmount its fixture too, so every command kind is covered.
        self.ensure_root()
        (self.root / self.smoke.ENOSPC_FILES[-1]).write_text("services: {}\n", encoding="utf-8")
        boundary = CleanupBoundary(self.root)
        with mock.patch.object(self.smoke.subprocess, "run", side_effect=boundary), \
             mock.patch.object(self.smoke.os.path, "ismount", return_value=True):
            self.smoke.cleanup(self.root)
        self.assertEqual(boundary.commands,
                         expected_cleanup_commands(self.root, self.smoke.ENOSPC_FILES, unmount=True))
        for command, kwargs in zip(boundary.commands, boundary.kwargs):
            with self.subTest(command=command[:3]):
                self.assertIsInstance(kwargs.get("timeout"), (int, float))
                self.assertGreater(kwargs["timeout"], 0)

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


class EffectiveProjectTests(RoundTwoCase):
    """Startup, teardown and the absence readback all use one pinned Compose project."""

    def leftover_after_teardown(self, environment: dict[str, str]) -> tuple[str, ComposeHostFake, Path]:
        root = self.parent / "smoke-root"
        host = ComposeHostFake(leave_on_down=True)
        self.prepare(root, host)
        with mock.patch.dict(os.environ, environment), \
             mock.patch.object(self.smoke.subprocess, "run", side_effect=host):
            self.smoke.Deployment(root).compose("up", "-d")
            try:
                self.smoke.cleanup(root)
                outcome = "verified"
            except self.smoke.SmokeError as exc:
                outcome = f"refused: {exc}"
        return outcome, host, root

    def test_inherited_project_name_cannot_hide_resources_left_by_teardown(self):
        outcome, host, root = self.leftover_after_teardown({"COMPOSE_PROJECT_NAME": "override-project"})
        self.assertRegex(outcome, "^refused: .*containers remain")
        self.assertFalse(host.removal_requested())
        self.assertTrue((root / "config" / "config.yaml").is_file())

    def test_every_compose_command_and_the_readback_use_the_same_project(self):
        _, host, _ = self.leftover_after_teardown({"COMPOSE_PROJECT_NAME": "override-project"})
        used = {ComposeHostFake.effective_project(command, kwargs)
                for command, kwargs in zip(host.commands, host.kwargs) if command[:2] == ["docker", "compose"]}
        self.assertEqual(used, {"smoke-root"})
        self.assertEqual(host.census_projects(), ["smoke-root"] * 3)

    def test_leftover_without_an_override_is_still_refused(self):
        # Control: the same leftover in the default project.
        with mock.patch.dict(os.environ):
            os.environ.pop("COMPOSE_PROJECT_NAME", None)
            outcome, host, root = self.leftover_after_teardown({})
        self.assertRegex(outcome, "^refused: .*containers remain")
        self.assertFalse(host.removal_requested())
        self.assertTrue(root.is_dir())


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


class ScenarioLocalDeadlineTests(unittest.TestCase):
    """Failure-path diagnostics and one-off container removal are bounded and cannot skip final cleanup."""

    def setUp(self) -> None:
        self.smoke = load_smoke()

    def test_admin_log_diagnostic_has_a_deadline_and_reports_its_failure(self):
        for failure in (subprocess.TimeoutExpired(["docker"], 1), OSError("synthetic launch failure")):
            with self.subTest(failure=type(failure).__name__):
                calls = []

                def hung(command, **kwargs):
                    calls.append(kwargs)
                    raise failure

                with mock.patch.object(self.smoke.subprocess, "run", side_effect=hung), \
                     mock.patch("sys.stderr"):
                    try:
                        self.smoke.print_admin_logs(self.smoke.Deployment(Path("synthetic-root")))
                        escaped = None
                    except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
                        escaped = type(exc).__name__
                self.assertIsNone(escaped)
                self.assertGreater(calls[0].get("timeout") or 0, 0)

    def test_interrupted_startup_container_removal_has_a_deadline(self):
        import io
        smoke = self.smoke
        phase = smoke.KILL_PHASES[0]

        class Process:
            def __init__(self, command, **kwargs):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(f"{smoke.KILL_MARKER} {phase}\n")
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = -9
                return self.returncode

            def kill(self):
                self.returncode = -9

        removals = []

        def boundary(command, **kwargs):
            command = list(command)
            if command[:3] == ["docker", "rm", "-f"]:
                removals.append(kwargs)
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout") or 0)
            return subprocess.CompletedProcess(command, 0, "", "")

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(os.path.realpath(directory.name))
        smoke._record_created_root(root)  # Compose runs only in a root this run created
        with mock.patch.object(smoke.subprocess, "Popen", Process), \
             mock.patch.object(smoke.subprocess, "run", side_effect=boundary), \
             mock.patch("sys.stderr"):
            try:
                smoke.kill_during_startup(smoke.Deployment(root), phase, "synthetic-kill")
                escaped = None
            except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
                escaped = type(exc).__name__
        self.assertIsNone(escaped)
        self.assertEqual(len(removals), 1)
        self.assertGreater(removals[0].get("timeout") or 0, 0)


def tree_snapshot(path: Path) -> dict[str, bytes | None]:
    """Every entry under path with its bytes (None for a directory)."""
    return {str(entry.relative_to(path)): (entry.read_bytes() if entry.is_file() else None)
            for entry in sorted(path.rglob("*"))}


class TreeHost(ComposeHostFake):
    """ComposeHostFake that also plays ``sudo mv`` and single-file ``sudo rm`` on the temporary tree."""

    def __call__(self, command, **kwargs):
        listed = list(command)
        if listed[:2] == ["sudo", "mv"]:
            self.commands.append(listed)
            self.kwargs.append(kwargs)
            Path(listed[2]).rename(listed[3])
            return subprocess.CompletedProcess(listed, 0, "" if kwargs.get("text") else b"", "")
        if listed[:2] == ["sudo", "rm"] and listed[2] != "-rf":
            self.commands.append(listed)
            self.kwargs.append(kwargs)
            Path(listed[-1]).unlink()
            return subprocess.CompletedProcess(listed, 0, "" if kwargs.get("text") else b"", "")
        return super().__call__(command, **kwargs)


class SwapHost(TreeHost):
    """TreeHost that moves the created root aside and puts a foreign tree at its path after command N."""

    def __init__(self, root: Path, foreign: Path, *, swap_after: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.root = root
        self.foreign = foreign
        self.swap_after = swap_after
        self.swapped = False

    def swap(self) -> None:
        self.root.rename(self.root.parent / "created-by-this-run")
        self.foreign.rename(self.root)
        self.swapped = True

    def __call__(self, command, **kwargs):
        result = super().__call__(command, **kwargs)
        if not self.swapped and self.swap_after is not None and len(self.commands) == self.swap_after:
            self.swap()
        return result


def stage_backup_target(root: Path, *, available: bool) -> None:
    """The backup-target state each target helper starts from: a target directory, or the held one plus a file."""
    targets = root / "backup-targets"
    targets.mkdir(exist_ok=True)
    if available:
        (targets / "remote").mkdir()
    else:
        (targets / "remote.held").mkdir()
        (targets / "remote").write_bytes(b"")


class RoundThreeCase(RoundTwoCase):
    def case(self, tag: str) -> tuple[Path, Path]:
        """A fresh holder with an absent root path and a synthetic operator tree beside it."""
        holder = self.parent / tag
        holder.mkdir(mode=0o755)
        foreign = holder / "operator-install"
        for name in ("config", "history", "backup-targets/remote"):
            (foreign / name).mkdir(parents=True)
        (foreign / ".env").write_text("OPERATOR ENV\n", encoding="utf-8")
        (foreign / "docker-compose.yml").write_text("OPERATOR COMPOSE\n", encoding="utf-8")
        (foreign / "config" / "config.yaml").write_text("operator config\n", encoding="utf-8")
        return holder / "smoke-root", foreign

    def attempt(self, action, host) -> tuple[str, mock.Mock]:
        """``completed``, ``refused: <reason>`` or ``unhandled <type>``, and the Popen recorder."""
        popen = mock.Mock(side_effect=AssertionError("a process was launched"))
        with mock.patch.object(self.smoke.subprocess, "run", side_effect=host), \
             mock.patch.object(self.smoke.subprocess, "Popen", popen), \
             mock.patch.object(self.smoke.os.path, "ismount", return_value=True), \
             mock.patch("sys.stderr"):
            try:
                action()
            except self.smoke.SmokeError as exc:
                return f"refused: {exc}", popen
            except Exception as exc:  # noqa: BLE001 - reported as the observed outcome
                return f"unhandled {type(exc).__name__}", popen
        return "completed", popen


class OwnershipCheckedWriteTests(RoundThreeCase):
    """Every write, move or removal inside a root passes the ownership check immediately before it."""

    def consumers(self, root: Path) -> dict:
        smoke = self.smoke

        def deployment(image: str | None = None):
            created = smoke.Deployment(root)
            created.image = image
            return created

        return {
            "Deployment.set_image": lambda: deployment().set_image("synthetic-image"),
            "Deployment.set_runtime_environment":
                lambda: deployment("synthetic-image").set_runtime_environment("SYNTHETIC", "value"),
            "Deployment.compose": lambda: deployment().compose("up", "-d"),
            "Deployment.exec_python": lambda: deployment().exec_python("enclosure-ui", "print(1)"),
            "Deployment.run_python": lambda: deployment().run_python("enclosure-history", "print(1)"),
            "kill_during_startup": lambda: smoke.kill_during_startup(deployment(), smoke.KILL_PHASES[0], "synthetic"),
            "install_enospc_history_mount": lambda: smoke.install_enospc_history_mount(deployment()),
            "install_readonly_history_mount": lambda: smoke.install_readonly_history_mount(deployment()),
            "install_backup_secrets": lambda: smoke.install_backup_secrets(root),
            "install_backup_scheduler_with_target": lambda: smoke.install_backup_scheduler_with_target(deployment()),
            "make_backup_target_unavailable": lambda: smoke.make_backup_target_unavailable(root),
            "make_backup_target_available": lambda: smoke.make_backup_target_available(root),
            "cleanup": lambda: smoke.cleanup(root),
        }

    def test_every_write_move_and_removal_refuses_a_replaced_root(self):
        names = list(self.consumers(self.parent / "unused"))
        for index, name in enumerate(names):
            with self.subTest(consumer=name):
                root, foreign = self.case(f"case-{index}")
                self.prepare(root, TreeHost())
                before = tree_snapshot(foreign)
                root.rename(root.parent / "created-by-this-run")
                foreign.rename(root)
                host = TreeHost()
                outcome, popen = self.attempt(self.consumers(root)[name], host)
                self.assertEqual(host.commands, [], "a command reached the replacement")
                popen.assert_not_called()
                self.assertRegex(outcome, "^refused: .*no longer the directory this run created")
                self.assertEqual(tree_snapshot(root), before, "the replacement tree was written to")

    def test_unchanged_root_control_reaches_every_consumer(self):
        # Passing control: on the directory this run created nothing is refused.
        for index, name in enumerate(self.consumers(self.parent / "unused")):
            if name in ("kill_during_startup", "Deployment.exec_python", "Deployment.run_python", "cleanup"):
                continue  # they need a running synthetic container or end the root; covered elsewhere
            with self.subTest(consumer=name):
                root, _ = self.case(f"control-{index}")
                self.prepare(root, TreeHost())
                stage_backup_target(root, available=name != "make_backup_target_available")
                outcome, _ = self.attempt(self.consumers(root)[name], TreeHost())
                self.assertEqual(outcome, "completed")

    def test_each_command_is_checked_immediately_before_it(self):
        # The root is replaced after the k-th command of a multi-step helper:
        # the next command or file write must not reach the replacement.
        smoke = self.smoke

        def registered(root: Path, *, available: bool = True) -> None:
            self.prepare(root, TreeHost())
            stage_backup_target(root, available=available)

        cases = {
            "prepare_root": (None, 12, lambda root: smoke.prepare_root(
                root, COMPOSE_FIXTURE, CONFIG_FIXTURE, NONROOT_FIXTURE)),
            "install_backup_scheduler_with_target": (registered, 7, lambda root: (
                smoke.install_backup_scheduler_with_target(smoke.Deployment(root)))),
            "install_backup_secrets": (registered, 2, lambda root: smoke.install_backup_secrets(root)),
            "make_backup_target_unavailable": (registered, 2, lambda root: smoke.make_backup_target_unavailable(root)),
            "make_backup_target_available": (lambda root: registered(root, available=False), 2,
                                             lambda root: smoke.make_backup_target_available(root)),
        }
        for name, (setup, total, action) in cases.items():
            for swap_after in range(1, total):
                with self.subTest(helper=name, swap_after=swap_after):
                    root, foreign = self.case(f"{name}-{swap_after}")
                    if setup is not None:
                        setup(root)
                    before = tree_snapshot(foreign)
                    host = SwapHost(root, foreign, swap_after=swap_after)
                    outcome, _ = self.attempt(lambda: action(root), host)
                    self.assertTrue(host.swapped)
                    self.assertEqual(len(host.commands), swap_after,
                                     f"commands reached the replacement: {host.commands[swap_after:]}")
                    self.assertRegex(outcome, "^refused: .*no longer the directory this run created")
                    self.assertEqual(tree_snapshot(root), before, "the replacement tree was written to")

    def test_cleanup_does_not_unmount_inside_a_root_replaced_during_teardown(self):
        root, foreign = self.case("enospc")
        self.prepare(root, TreeHost())
        (root / self.smoke.ENOSPC_FILES[-1]).write_text("services: {}\n", encoding="utf-8")
        (foreign / self.smoke.ENOSPC_FILES[-1]).write_text("services: {}\n", encoding="utf-8")
        before = tree_snapshot(foreign)
        host = SwapHost(root, foreign, swap_after=1)
        outcome, _ = self.attempt(lambda: self.smoke.cleanup(root), host)
        self.assertEqual(len(host.commands), 1, host.commands)
        self.assertRegex(outcome, "^refused: .*no longer the directory this run created")
        self.assertFalse(any(command[:2] == ["sudo", "umount"] for command in host.commands))
        self.assertEqual(tree_snapshot(root), before)

    def test_interrupted_migration_reference_copy_refuses_a_root_replaced_by_a_step(self):
        smoke = self.smoke
        root, foreign = self.case("interrupted")
        before = tree_snapshot(foreign)
        args = smoke.build_parser().parse_args(cli_argv(root, scenario="interrupted-migration"))
        # prepare_root issues seven commands, then the scenario stops history and
        # pulls the candidate; the root is replaced right after that pull.
        host = SwapHost(root, foreign, swap_after=9)
        with mock.patch.object(smoke, "seed_previous_release", return_value=({}, {}, {})):
            outcome, _ = self.attempt(lambda: smoke.interrupted_migration(args), host)
        self.assertTrue(host.swapped)
        self.assertEqual(host.commands[8][-1:], ["pull"])
        self.assertEqual(len(host.commands), 9, f"commands reached the replacement: {host.commands[9:]}")
        self.assertRegex(outcome, "^refused: .*no longer the directory this run created")
        self.assertEqual(tree_snapshot(root), before)


class SharedAncestorTests(RoundThreeCase):
    """A root is refused when another unprivileged user could rename entries above or in it."""

    REASON = "could let another user rename or replace"

    def holder(self, name: str, mode: int, parent: Path | None = None) -> Path:
        holder = (parent or self.parent) / name
        holder.mkdir()
        os.chmod(holder, mode)
        self.addCleanup(os.chmod, holder, 0o755)
        return holder

    def test_root_below_a_directory_others_can_rename_entries_in_is_refused(self):
        for mode in (0o777, 0o775, 0o757, 0o770):
            with self.subTest(mode=oct(mode)):
                holder = self.holder(f"shared-{mode:o}", mode)
                host = TreeHost()
                outcome, _ = self.attempt(
                    lambda: self.smoke.prepare_root(holder / "smoke-root", COMPOSE_FIXTURE, CONFIG_FIXTURE), host)
                self.assertRegex(outcome, f"^refused: .*{self.REASON}")
                self.assertEqual(host.commands, [])
                self.assertFalse((holder / "smoke-root").exists())

    def test_shared_grandparent_is_refused_too(self):
        grand = self.holder("shared-grandparent", 0o777)
        holder = self.holder("private", 0o755, grand)
        host = TreeHost()
        outcome, _ = self.attempt(
            lambda: self.smoke.prepare_root(holder / "smoke-root", COMPOSE_FIXTURE, CONFIG_FIXTURE), host)
        self.assertRegex(outcome, f"^refused: .*{self.REASON}")
        self.assertEqual(host.commands, [])
        self.assertFalse((holder / "smoke-root").exists())

    def test_parent_owned_by_another_user_is_refused(self):
        holder = self.holder("foreign-owner", 0o755)
        real_lstat = os.lstat
        stranger = os.geteuid() + 4242

        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if Path(path) == holder:
                fields = list(info[:10])
                fields[4] = stranger  # st_uid
                return os.stat_result(fields)
            return info

        host = TreeHost()
        with mock.patch.object(self.smoke.os, "lstat", side_effect=lstat):
            outcome, _ = self.attempt(
                lambda: self.smoke.prepare_root(holder / "smoke-root", COMPOSE_FIXTURE, CONFIG_FIXTURE), host)
        self.assertRegex(outcome, f"^refused: .*{self.REASON}")
        self.assertEqual(host.commands, [])
        self.assertFalse((holder / "smoke-root").exists())

    def test_sticky_and_private_parents_are_admitted(self):
        # Passing control: in a sticky directory another user cannot rename this run's entry.
        for mode in (0o1777, 0o1775, 0o755, 0o700):
            with self.subTest(mode=oct(mode)):
                holder = self.holder(f"ok-{mode:o}", mode)
                outcome, _ = self.attempt(
                    lambda: self.smoke.prepare_root(holder / "smoke-root", COMPOSE_FIXTURE, CONFIG_FIXTURE),
                    TreeHost())
                self.assertEqual(outcome, "completed")
                self.assertTrue(self.smoke.owns_root(holder / "smoke-root"))

    def test_a_parent_opened_to_other_users_after_creation_stops_every_write_and_removal(self):
        holder = self.holder("later-shared", 0o755)
        root = holder / "smoke-root"
        self.prepare(root, TreeHost())
        os.chmod(holder, 0o777)
        for name, action in (("set_image", lambda: self.smoke.Deployment(root).set_image("synthetic")),
                             ("cleanup", lambda: self.smoke.cleanup(root))):
            with self.subTest(action=name):
                host = TreeHost()
                outcome, _ = self.attempt(action, host)
                self.assertRegex(outcome, f"^refused: .*{self.REASON}")
                self.assertEqual(host.commands, [])
                self.assertFalse((root / ".env").exists())
                self.assertTrue((root / "config" / "config.yaml").is_file())

    def test_clean_host_root_below_a_shared_directory_is_refused(self):
        holder = self.holder("shared-clean-host", 0o777)
        root = holder / "jbod-clean-host-shared"
        with self.assertRaisesRegex(self.smoke.SmokeError, self.REASON):
            self.smoke.admit_clean_host_root(root, consent=True)
        self.assertFalse(root.exists())


class CreationAcquisitionTests(RoundThreeCase):
    def test_a_populated_directory_found_at_creation_is_not_recorded(self):
        root, foreign = self.case("acquire")
        before = tree_snapshot(foreign)
        real_mkdir = os.mkdir

        def mkdir_then_swap(path, mode=0o777, *args, **kwargs):
            real_mkdir(path, mode, *args, **kwargs)
            if Path(path) == root:
                root.rename(root.parent / "created-by-this-run")
                foreign.rename(root)

        host = TreeHost()
        with mock.patch.object(self.smoke.os, "mkdir", side_effect=mkdir_then_swap):
            outcome, _ = self.attempt(lambda: self.smoke.prepare_root(root, COMPOSE_FIXTURE, CONFIG_FIXTURE), host)
        self.assertRegex(outcome, "^refused: .*not the empty directory this run just created")
        self.assertEqual(host.commands, [])
        self.assertEqual(tree_snapshot(root), before)
        self.assertFalse(self.smoke.owns_root(root))


    def test_a_directory_open_to_other_users_found_at_creation_is_not_recorded(self):
        # The root is recorded only while it is private, not first and refused later.
        root, _ = self.case("acquire-open")
        real_mkdir = os.mkdir

        def mkdir_open(path, mode=0o777, *args, **kwargs):
            real_mkdir(path, 0o755 if Path(path) == root else mode, *args, **kwargs)

        host = TreeHost()
        with mock.patch.object(self.smoke.os, "mkdir", side_effect=mkdir_open):
            outcome, _ = self.attempt(lambda: self.smoke.prepare_root(root, COMPOSE_FIXTURE, CONFIG_FIXTURE), host)
        self.assertRegex(outcome, "^refused: .*not the empty directory this run just created")
        self.assertEqual(host.commands, [])
        self.assertNotIn(root, self.smoke._OWNED_ROOTS)


class NestedRootRemovalTests(RoundThreeCase):
    """Nothing is removed recursively until every root this run created beneath it passes its own check."""

    def nested(self, tag: str) -> tuple[Path, Path, Path]:
        outer, foreign = self.case(tag)
        self.prepare(outer, TreeHost())
        child = outer / "child-root"
        self.prepare(child, TreeHost())
        return outer, child, foreign

    def test_replaced_child_root_stops_the_recursive_removal_of_its_parent(self):
        outer, child, foreign = self.nested("replaced-child")
        child.rename(outer / "child-created-by-run")
        foreign.rename(child)
        before = tree_snapshot(child)
        host = TreeHost()
        outcome, _ = self.attempt(lambda: self.smoke.cleanup(outer), host)
        self.assertRegex(outcome, "^refused: .*no longer the directory this run created")
        self.assertFalse(host.removal_requested())
        self.assertEqual(tree_snapshot(child), before)

    def test_unchanged_child_roots_are_removed_and_forgotten_with_their_parent(self):
        # Passing control.
        outer, child, _ = self.nested("unchanged-child")
        host = TreeHost()
        outcome, _ = self.attempt(lambda: self.smoke.cleanup(outer), host)
        self.assertEqual(outcome, "completed")
        self.assertFalse(outer.exists())
        self.assertNotIn(child, self.smoke._OWNED_ROOTS)
        self.assertNotIn(outer, self.smoke._OWNED_ROOTS)


class HandoffHost(TreeHost):
    """TreeHost that models the hardened ownership handoff, operand by operand, without changing any owner.

    Each ``chown`` operand (direct, recursive or from ``find -exec``) is
    resolved the way the kernel resolves it for chown: through every
    intermediate component, and through a final symlink unless ``-h`` is given.
    A directory handed to the app identity is then writable by that other user.
    Before each later operand that user replaces the entry on that operand's
    path inside a directory it was handed with a symlink to an outside file, if
    it can reach that directory: every directory between the shared parent and
    it is either its own or searchable by others. ``reach_always`` grants the
    reach regardless, to test the ownership step on its own. A recursive
    ``chown -R`` without ``-H``/``-L`` walks by descriptor and never follows a
    link, so its descendants are taken as one step. The handoff is modelled:
    no owner, mode or privilege changes.
    """

    def __init__(self, root: Path, shared: Path, outside: Path, *, reach_always: bool = False,
                 attacks: bool = True, populate=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.root, self.shared, self.outside = root, shared, outside
        self.reach_always = reach_always
        self.attacks = attacks
        self.populate = populate
        self.handed: set[Path] = set()
        self.targets: list[tuple[Path, str]] = []
        self.installs: list[tuple[Path, str, str]] = []
        self.swaps: list[Path] = []
        self.root_modes: list[int] = []

    def done(self, listed, kwargs):
        self.commands.append(listed)
        self.kwargs.append(kwargs)
        return subprocess.CompletedProcess(listed, 0, "" if kwargs.get("text") else b"", "")

    def __call__(self, command, **kwargs):
        listed = list(command)
        if os.path.isdir(self.root):
            self.root_modes.append(os.lstat(self.root).st_mode)
        cwd = Path(kwargs.get("cwd") or os.getcwd())
        if listed[:1] == ["bash"]:
            self.ownership_started()
            for line in listed[-1].splitlines():
                self.interpret_line(line, cwd)
            return self.done(listed, kwargs)
        if listed[:2] in (["sudo", "find"], ["sudo", "chown"]):
            self.ownership_started()
            self.privileged(listed, cwd)
            return self.done(listed, kwargs)
        if listed[:3] == ["sudo", "install", "-d"] and "-o" in listed and os.path.isdir(cwd / listed[-1]):
            # An ownership change of an existing directory, as in the hardened step.
            self.record_install(listed, cwd)
            return self.done(listed, kwargs)
        return super().__call__(command, **kwargs)

    def ownership_started(self) -> None:
        if self.populate is not None:
            self.populate(self.root)
            self.populate = None

    def interpret_line(self, line: str, cwd: Path) -> None:
        """One line of the documented shell step; ``name="${NAME:-default}"`` reads the environment like the shell."""
        import re
        import shlex
        assignment = re.fullmatch(r'(\w+)="\$\{(\w+):-(\d+)\}"', line)
        if assignment:
            value = os.environ.get(assignment[2]) or assignment[3]
            self.variables = {**getattr(self, "variables", {}), assignment[1]: value}
            return
        words = [re.sub(r"\$(\w+)", lambda match: self.variables[match[1]], word) for word in shlex.split(line)]
        if words[:3] == ["sudo", "install", "-d"]:
            self.record_install(words, cwd)
        else:
            self.privileged(words, cwd)

    def record_install(self, words: list[str], cwd: Path) -> None:
        self.attack(cwd / words[-1])
        target = Path(os.path.realpath(cwd / words[-1]))
        owner = f"{words[words.index('-o') + 1]}:{words[words.index('-g') + 1]}"
        self.installs.append((target, owner, words[words.index("-m") + 1]))

    def privileged(self, words: list[str], cwd: Path) -> None:
        import fnmatch
        if words[1] == "find":
            rest = words[2:]
            while rest[0] in ("-P", "-H", "-L"):
                rest = rest[1:]
            start, expression = rest[0], rest[1:]
            exec_at = expression.index("-exec")
            chown = expression[exec_at:expression.index("{}")][1:]
            patterns = [expression[i + 1] for i, word in enumerate(expression) if word == "-path"]
            depth, prune = "-depth" in expression, "-prune" in expression
            operands = []

            def visit(relative: str) -> None:
                excluded = any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)
                path = cwd / relative
                descend = path.is_dir() and not path.is_symlink() and not (excluded and prune and not depth)
                if not depth and not excluded:
                    operands.append(relative)
                if descend:
                    for name in sorted(os.listdir(path)):
                        visit(f"{relative}/{name}")
                if depth and not excluded:
                    operands.append(relative)

            visit(start)
            self.chown(chown, operands, cwd)
        elif words[1] == "chown":
            self.chown(words[1:], None, cwd)
        else:
            raise AssertionError(f"unexpected privileged command {words}")

    def chown(self, words: list[str], operands: list[str] | None, cwd: Path) -> None:
        flags = {word for word in words[1:] if word.startswith("-") and word != "--"}
        positional = [word for word in words[1:] if not word.startswith("-")]
        identity, named = positional[0], positional[1:]
        recursive = "-R" in flags
        follow_final = "-h" not in flags and not (recursive and not flags & {"-H", "-L"})
        for relative in (named if operands is None else operands):
            self.attack(cwd / relative)
            path = Path(os.path.normpath(cwd / relative))
            target = Path(os.path.realpath(path)) if follow_final else Path(os.path.realpath(path.parent)) / path.name
            self.hand(target, identity)
            if recursive and target.is_dir() and not target.is_symlink():
                for directory, names, files in os.walk(target):
                    for name in sorted(names + files):
                        entry = Path(directory) / name
                        # -H/-L make the walk follow a link below an operand too.
                        self.hand(Path(os.path.realpath(entry)) if flags & {"-H", "-L"} else entry, identity)

    def hand(self, target: Path, identity: str) -> None:
        self.targets.append((target, identity))
        if target.is_dir() and not target.is_symlink() and target.is_relative_to(self.root):
            self.handed.add(target)

    def reachable(self, directory: Path) -> bool:
        if self.reach_always:
            return True
        for component in [directory, *directory.parents]:
            if component == self.shared:
                return True
            if component != directory and component not in self.handed \
                    and not os.lstat(component).st_mode & stat.S_IXOTH:
                return False
        return False

    def attack(self, pending: Path) -> None:
        if not self.attacks:
            return
        pending = Path(os.path.normpath(pending))
        for directory in sorted(self.handed, key=lambda path: len(path.parts)):
            if pending == directory or not pending.is_relative_to(directory) or not self.reachable(directory):
                continue
            entry = directory / pending.relative_to(directory).parts[0]
            if entry.is_symlink() or not os.path.lexists(entry):
                continue
            entry.rename(entry.with_name(entry.name + ".before-swap"))
            entry.symlink_to(self.outside if entry == pending else self.outside.parent)
            self.swaps.append(entry)
            return

    def outside_targets(self) -> list[Path]:
        return [target for target, _ in self.targets if not target.is_relative_to(self.root)]


class DescendantHandoffTests(RoundThreeCase):
    """A directory handed to another user never lets that user redirect a later privileged step."""

    def setUp(self) -> None:
        super().setUp()
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("APP_UID", "APP_GID", "BACKUP_UID"):
            os.environ.pop(name, None)
        self.shared = self.parent / "shared"
        self.shared.mkdir()
        os.chmod(self.shared, 0o1777)  # sticky and shared, like /tmp: admitted
        self.addCleanup(os.chmod, self.shared, 0o755)
        (self.parent / "outside").mkdir()
        self.outside = self.parent / "outside" / "operator-file"
        self.outside.write_text("synthetic, never handed over\n", encoding="utf-8")

    def hardened(self, root: Path, **kwargs) -> tuple[str, HandoffHost]:
        host = HandoffHost(root, self.shared, self.outside, **kwargs)
        outcome, _ = self.attempt(
            lambda: self.smoke.prepare_root(root, COMPOSE_FIXTURE, CONFIG_FIXTURE, NONROOT_FIXTURE), host)
        return outcome, host

    def test_the_app_identity_cannot_reach_what_it_was_handed_while_ownership_changes(self):
        outcome, host = self.hardened(self.shared / "smoke-root")
        self.assertEqual(outcome, "completed")
        self.assertTrue(host.targets, "the ownership step did not run")
        self.assertEqual(host.swaps, [], "another user reached a directory it had been handed")
        self.assertEqual(host.outside_targets(), [], "an ownership change resolved outside the root")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "synthetic, never handed over\n")

    def test_the_ownership_step_follows_no_link_even_where_that_user_could_reach(self):
        # The step on its own, with the private root assumed away: a nested entry
        # waits below a directory that could be handed over first, and a link
        # already inside ./config must be changed itself, not its referent.
        def populate(root: Path) -> None:
            (root / "config" / "ssh" / "known_hosts").write_text("synthetic\n", encoding="utf-8")
            (root / "config" / "planted-link").symlink_to(self.outside)
            (root / "data" / "planted-link").symlink_to(self.outside)

        outcome, host = self.hardened(self.shared / "smoke-root", reach_always=True, populate=populate)
        self.assertEqual(outcome, "completed")
        self.assertTrue(host.targets)
        self.assertEqual(host.outside_targets(), [], f"swapped entries: {host.swaps}")
        # Each ownership change is its own checked command, not one check around a shell.
        self.assertEqual([command for command in host.commands if command[:1] in (["bash"], ["sh"])], [])

    def test_the_root_stays_private_to_this_user_for_every_privileged_command(self):
        root = self.shared / "smoke-root"
        outcome, host = self.hardened(root)
        self.assertEqual(outcome, "completed")
        self.assertTrue(host.root_modes)
        self.assertEqual({oct(stat.S_IMODE(mode) & 0o077) for mode in host.root_modes}, {"0o0"})
        self.assertEqual(stat.S_IMODE(os.lstat(root).st_mode) & 0o077, 0)

    def test_a_root_opened_to_other_users_stops_the_next_write_and_removal(self):
        root = self.shared / "smoke-root"
        self.prepare(root, TreeHost())
        os.chmod(root, 0o755)
        self.addCleanup(lambda: os.path.isdir(root) and os.chmod(root, 0o700))
        for name, action in (("set_image", lambda: self.smoke.Deployment(root).set_image("synthetic")),
                             ("cleanup", lambda: self.smoke.cleanup(root))):
            with self.subTest(action=name):
                host = TreeHost()
                outcome, _ = self.attempt(action, host)
                self.assertRegex(outcome, "^refused: .*open to other users")
                self.assertEqual(host.commands, [])
                self.assertFalse((root / ".env").exists())

    def test_symlink_safe_step_changes_the_same_paths_to_the_same_owners_as_the_documented_one(self):
        # Healthy control: on an ordinary tree the script's step and the
        # documented shell step change exactly the same entries, and both
        # leave config/backup-secrets to the backup identity.
        def populate(root: Path) -> None:
            for name in ("config/ssh", "config/backup-secrets", "data/sub"):
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in ("config/ssh/known_hosts", "config/backup-secrets/backup-passphrase", "data/sub/file"):
                (root / name).write_text("synthetic\n", encoding="utf-8")

        root = self.shared / "smoke-root"
        outcome, host = self.hardened(root, populate=populate, attacks=False)
        self.assertEqual(outcome, "completed")
        documented_root = self.shared / "documented-root"
        self.prepare(documented_root, TreeHost())
        populate(documented_root)
        documented = HandoffHost(documented_root, self.shared, self.outside, attacks=False)
        for line in self.smoke.HARDENED_OWNERSHIP_PREP:
            documented.interpret_line(line, documented_root)

        def relative(recorded, base):
            return sorted((str(target.relative_to(base)), *rest) for target, *rest in recorded)

        self.assertEqual(relative(host.targets, root), relative(documented.targets, documented_root))
        self.assertEqual(relative(host.installs, root), relative(documented.installs, documented_root))
        self.assertNotIn("config/backup-secrets", [path for path, _ in relative(host.targets, root)])
        self.assertIn(("config/config.yaml", "10001:10001"), relative(host.targets, root))

    def test_identities_follow_the_documented_defaults_and_are_refused_unless_numeric(self):
        root = self.shared / "smoke-root"
        os.environ.update(APP_UID="20001", APP_GID="", BACKUP_UID="1000")
        outcome, host = self.hardened(root)
        self.assertEqual(outcome, "completed")
        self.assertEqual({identity for _, identity in host.targets}, {"20001:10001"})
        os.environ["APP_UID"] = "app user"
        outcome, host = self.hardened(self.shared / "named-root")
        self.assertRegex(outcome, "^refused: APP_UID")
        self.assertEqual(host.commands, [])
        self.assertFalse(os.path.lexists(self.shared / "named-root"))


class ThreatModelTests(unittest.TestCase):
    SCRIPT = REPOSITORY / "scripts" / "run_image_upgrade_smoke.py"
    # Privileged commands that only read; every other one is a write, move or removal.
    READ_VERBS = {"cat", "stat", "find"}
    # The only functions allowed to write in-process or to issue a privileged
    # write: each performs the ownership check itself, immediately before it.
    CHECKED_WRITERS = {"owned_run", "owned_write", "owned_unlink", "owned_mkdir",
                       "_create_owned_directory", "remove_owned_tree"}
    WRITE_METHODS = {"write_text", "write_bytes", "chmod", "unlink", "mkdir", "rename", "touch", "symlink_to",
                     "rmdir"}
    WRITE_FUNCTIONS = {("shutil", "copyfile"), ("shutil", "copy"), ("shutil", "move"), ("shutil", "rmtree"),
                       ("os", "open"), ("os", "mkdir"), ("os", "rename"), ("os", "replace"), ("os", "remove"),
                       ("os", "unlink"), ("os", "chmod"), ("os", "rmdir")}

    def test_script_header_states_its_threat_model(self):
        doc = " ".join((load_smoke().__doc__ or "").split())
        self.assertIn("other unprivileged users", doc)
        self.assertIn("does not claim protection against a process running as the same user or as root", doc)

    def test_no_write_move_or_removal_bypasses_the_ownership_check(self):
        import ast
        tree = ast.parse(self.SCRIPT.read_text(encoding="utf-8"))
        offenders = []
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef) or function.name in self.CHECKED_WRITERS:
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func
                if isinstance(callee, ast.Name) and callee.id in ("run", "run_bytes") and node.args \
                        and isinstance(node.args[0], ast.List) and node.args[0].elts:
                    words = [element.value for element in node.args[0].elts[:2] if isinstance(element, ast.Constant)]
                    privileged_write = words[:1] == ["sudo"] and words[1:2] and words[1] not in self.READ_VERBS
                    # find is a read only without an action that changes what it finds.
                    privileged_write = privileged_write or words[:2] == ["sudo", "find"] and any(
                        isinstance(element, ast.Constant) and element.value in ("-exec", "-execdir", "-ok", "-delete")
                        for element in node.args[0].elts)
                    if words[:1] == ["bash"] or privileged_write:
                        offenders.append(f"{function.name}: {' '.join(map(str, words))}")
                elif isinstance(callee, ast.Attribute) and (
                        callee.attr in self.WRITE_METHODS
                        # Path.replace(target) moves a file; str.replace(old, new) takes two arguments.
                        or (callee.attr == "replace" and len(node.args) == 1)):
                    offenders.append(f"{function.name}: .{callee.attr}()")
                elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name) \
                        and (callee.value.id, callee.attr) in self.WRITE_FUNCTIONS:
                    offenders.append(f"{function.name}: {callee.value.id}.{callee.attr}()")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
