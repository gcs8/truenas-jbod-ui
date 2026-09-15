from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"

# `scripts/public_demo_source_parity.py` is a pinned public-demo source input
# (`scripts/public_demo_inputs.py`); changing it changes the published artifact
# digest, so its `--help` failure is tracked on #446 and excluded here by name.
KNOWN_UNRUNNABLE = {"public_demo_source_parity.py"}

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


def _run_help(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(path), "--help"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )


class ScriptHelpTests(unittest.TestCase):
    def test_every_script_prints_usage_on_help(self) -> None:
        failures: list[str] = []
        for path in _script_paths():
            if path.name in KNOWN_UNRUNNABLE:
                continue
            with self.subTest(script=path.name):
                result = _run_help(path)
                if result.returncode != 0:
                    failures.append(f"{path.name}: exit {result.returncode}: {result.stderr.strip()[-200:]}")
                    continue
                if "usage" not in result.stdout.lower():
                    failures.append(f"{path.name}: no usage line in stdout")
        self.assertEqual(failures, [], "\n".join(failures))

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
                described = False
                for line in text.splitlines():
                    stripped = line.strip()
                    if not stripped.startswith(argument):
                        continue
                    remainder = stripped[len(argument):]
                    # argparse puts the metavar right after the flag; help text
                    # follows after at least two spaces, or on the next line.
                    if "  " in remainder and remainder.split("  ", 1)[1].strip():
                        described = True
                if not described:
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
