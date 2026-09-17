from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"

# Every script in `scripts/` must survive `--help`, with no exclusions: #446.
# `public_demo_source_parity.py` was the last exception; it now carries the same
# repository-root `sys.path` bootstrap as `build_public_demo.py`, so the sweep
# below covers all of them. Keep this mapping empty — an entry here is a gap in
# the #446 claim, not a workaround.
KNOWN_UNRUNNABLE: dict[str, str] = {}

# Arguments an operator has to supply a value for, and a word the help text
# must explain them with.
REQUIRED_ARGUMENT_HELP = {
    "dev_check.py": ("--safe", "--full"),
    "seal_history_segment.py": ("--source", "--output-dir", "--segment-id", "--cutoff", "--key-id"),
    "rotate_segmented_history.py": ("--source", "--segments-dir", "--cutoff", "--key-id"),
    "migrate_segmented_history.py": ("--source", "--segments-dir", "--cutoff", "--key-id"),
    "check_public_demo_artifact.py": ("--max-raw-bytes", "--max-gzip-bytes"),
    "check_public_screenshots.py": ("--root",),
    "render_release_notes.py": ("--changelog",),
}

# Scripts whose POSIX-only imports must not run before argparse handles --help.
POSIX_ONLY_SCRIPTS = (
    "migrate_segmented_history.py",
    "rotate_segmented_history.py",
    "benchmark_full_backup.py",
)

BLOCK_POSIX_MODULES = """
import builtins, runpy, sys
blocked = {'fcntl', 'resource', 'grp', 'pwd'}
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in blocked:
        raise ModuleNotFoundError("No module named %r" % name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
target = sys.argv.pop(1)
sys.argv[0] = target
runpy.run_path(target, run_name='__main__')
"""


def _script_paths() -> list[Path]:
    return sorted(path for path in SCRIPTS_DIR.glob("*.py"))


def _is_command_line_entry_point(path: Path) -> bool:
    """True for scripts an operator runs; the shared data modules have no main guard."""
    return 'if __name__ == "__main__":' in path.read_text(encoding="utf-8")


def _argument_help_text(help_output: str, argument: str) -> str:
    """Return the help text argparse printed for one flag, joined across wraps."""
    lines = help_output.splitlines()
    # Skip the usage block: it repeats every flag with no description.
    for offset, line in enumerate(lines):
        if not line.strip():
            lines = lines[offset + 1:]
            break
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(argument):
            continue
        indent = len(line) - len(line.lstrip())
        remainder = stripped[len(argument):]
        parts: list[str] = []
        if "  " in remainder:
            parts.append(remainder.split("  ", 1)[1].strip())
        for continuation in lines[index + 1:]:
            if not continuation.strip():
                break
            continuation_indent = len(continuation) - len(continuation.lstrip())
            if continuation_indent <= indent:
                break
            parts.append(continuation.strip())
        return " ".join(part for part in parts if part).strip()
    return ""


def _run_help(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(path), "--help"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=True,
        timeout=120,
    ).stdout


def _tree_state() -> tuple[str, dict[str, tuple[int, int]]]:
    """What git sees, to prove `--help` writes nothing an operator would keep.

    Ignored runtime paths (`logs/*.log`) are out of scope on purpose: importing
    the app configures file logging, which is the app's behaviour, not the help
    path's. Anything git would report is in scope.
    """
    status = _git("status", "--porcelain")
    tracked: dict[str, tuple[int, int]] = {}
    for name in _git("ls-files", "-z").split("\x00"):
        if not name:
            continue
        try:
            stat = (ROOT / name).stat()
        except OSError:
            continue
        tracked[name] = (stat.st_size, stat.st_mtime_ns)
    return status, tracked


class ScriptHelpTests(unittest.TestCase):
    def test_the_help_sweep_excludes_nothing(self) -> None:
        """#446 is "every script", so an exclusion list defeats the claim."""
        self.assertEqual(KNOWN_UNRUNNABLE, {}, "the --help sweep must cover every script")

    def test_the_public_demo_source_parity_module_is_importable_as_a_script(self) -> None:
        """It is a library module, but running it directly must not explode."""
        result = _run_help(SCRIPTS_DIR / "public_demo_source_parity.py")
        self.assertEqual(result.returncode, 0, result.stderr.strip()[-400:])

    def test_every_script_prints_usage_on_help_without_side_effects(self) -> None:
        failures: list[str] = []
        before = _tree_state()
        for path in _script_paths():
            with self.subTest(script=path.name):
                result = _run_help(path)
                expected_failure = KNOWN_UNRUNNABLE.get(path.name)
                if expected_failure is not None:
                    # Pinned, not skipped: the reason has to stay exactly this one.
                    if result.returncode == 0 or expected_failure not in result.stderr:
                        failures.append(
                            f"{path.name}: expected the known {expected_failure!r} failure, "
                            f"got exit {result.returncode}; remove it from KNOWN_UNRUNNABLE"
                        )
                    continue
                if result.returncode != 0:
                    failures.append(f"{path.name}: exit {result.returncode}: {result.stderr.strip()[-200:]}")
                    continue
                if _is_command_line_entry_point(path) and "usage" not in result.stdout.lower():
                    failures.append(f"{path.name}: no usage line in stdout")
        self.assertEqual(failures, [], "\n".join(failures))
        after_status, after_tracked = _tree_state()
        before_status, before_tracked = before
        self.assertEqual(after_status, before_status, "running --help changed what git reports")
        touched = sorted(
            name
            for name in set(before_tracked) | set(after_tracked)
            if before_tracked.get(name) != after_tracked.get(name)
        )
        self.assertEqual(touched, [], "running --help rewrote tracked files")

    def test_help_explains_the_arguments_operators_must_fill_in(self) -> None:
        missing: list[str] = []
        for name, arguments in REQUIRED_ARGUMENT_HELP.items():
            path = SCRIPTS_DIR / name
            self.assertTrue(path.exists(), f"{name} is missing")
            result = _run_help(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = result.stdout
            for argument in arguments:
                self.assertIn(argument, text, f"{name} does not offer {argument}")
                if not _argument_help_text(text, argument):
                    missing.append(f"{name} {argument}")
        self.assertEqual(missing, [], "\n".join(missing))


    def test_help_works_without_the_posix_only_modules(self) -> None:
        for name in POSIX_ONLY_SCRIPTS:
            with self.subTest(script=name):
                result = subprocess.run(
                    [sys.executable, "-c", BLOCK_POSIX_MODULES, str(SCRIPTS_DIR / name), "--help"],
                    capture_output=True,
                    text=True,
                    cwd=str(ROOT),
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
