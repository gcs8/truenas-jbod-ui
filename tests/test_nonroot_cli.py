from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/prepare_nonroot_bind_mounts.py"


class NonRootCliTests(unittest.TestCase):
    def _run(self, arguments: list[str], *, unsupported: str = "", identity_absent: bool = False):
        import subprocess
        import sys

        # A subprocess prevents dependency/platform simulation leaking into the suite.
        bootstrap = """
import builtins, os, runpy, sys
original_import = builtins.__import__
mode = sys.argv.pop(1)
def guarded_import(name, *args, **kwargs):
    if name == 'resource' and mode == 'resource':
        raise ModuleNotFoundError('resource unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
if mode == 'identity' and hasattr(os, 'geteuid'):
    del os.geteuid
if mode == 'descriptors':
    os.supports_dir_fd = set()
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        if identity_absent:
            bootstrap = "import os\nif hasattr(os, 'geteuid'): del os.geteuid\n" + bootstrap
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = subprocess.run(
                [sys.executable, "-B", "-c", bootstrap, unsupported, str(SCRIPT_PATH), *arguments],
                cwd=root, text=True, capture_output=True, check=False, timeout=30,
            )
            self.assertEqual(list(root.iterdir()), [])
            return result

    def test_help_precedes_unavailable_posix_dependencies(self) -> None:
        for mode in ("resource", "identity", "descriptors"):
            with self.subTest(mode=mode):
                result = self._run(["--help"], unsupported=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
                self.assertIn("10001", result.stdout)
                self.assertIn("preflight", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_unsupported_execution_fails_before_inspecting_root(self) -> None:
        for mode in ("resource", "identity", "descriptors"):
            for apply in ([], ["--apply"]):
                with self.subTest(mode=mode, apply=apply):
                    result = self._run(["does-not-exist", *apply], unsupported=mode)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("unsupported", result.stderr)
                    self.assertIn("deployment host", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertNotIn("docker compose", result.stderr)
                    self.assertEqual(result.stdout, "")


    def test_identity_simulation_accepts_already_absent_geteuid(self) -> None:
        result = self._run(["--help"], unsupported="identity", identity_absent=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
