from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import run_compose_runtime_matrix, run_private_qa_restore, update_immutable_deployment

ROOT = Path(__file__).resolve().parents[1]

# Runs a script as a file with the POSIX locking module missing, the way a
# Windows checkout sees it, and captures what the user would read.
_WITHOUT_FCNTL = """
import runpy, sys
sys.modules["fcntl"] = None
sys.argv = [sys.argv[1], "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


class ScriptPlatformGuardTests(unittest.TestCase):
    def test_history_tools_explain_the_linux_requirement_instead_of_a_traceback(self) -> None:
        for name in ("migrate_segmented_history.py", "rotate_segmented_history.py"):
            with self.subTest(script=name):
                result = subprocess.run(
                    [sys.executable, "-c", _WITHOUT_FCNTL, str(ROOT / "scripts" / name)],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("This tool needs Linux.", result.stderr)
                self.assertIn(f"docker compose run --rm enclosure-history python /app/scripts/{name}", result.stderr)

    def test_immutable_updater_explains_a_missing_process_identity(self) -> None:
        fake_os = MagicMock()
        del fake_os.geteuid
        with patch.object(update_immutable_deployment, "os", fake_os):
            with self.assertRaisesRegex(update_immutable_deployment.DeploymentError, "needs a Linux Docker host"):
                update_immutable_deployment._effective_uid()

    def test_docker_host_harnesses_explain_a_missing_proc_meminfo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            absent = Path(raw) / "meminfo"
            present = Path(raw) / "present"
            present.write_text("MemAvailable: 1 kB\n", encoding="utf-8")
            for module in (run_compose_runtime_matrix, run_private_qa_restore):
                with self.subTest(module=module.__name__):
                    with self.assertRaisesRegex(SystemExit, "runs on a Linux Docker"):
                        module._require_linux_docker_host(meminfo=absent)
                    module._require_linux_docker_host(meminfo=present)

    def test_source_parity_module_imports_when_run_as_a_file(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "public_demo_source_parity.py")],
            cwd=ROOT / "scripts",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_maintainer_check_scripts_describe_their_arguments(self) -> None:
        expectations = {
            "check_public_demo_artifact.py": ("Directory holding the checked-in index.html", "Largest index.html allowed"),
            "check_public_screenshots.py": ("Repository checkout to verify",),
            "render_release_notes.py": ("CHANGELOG.md to read",),
            "dev_check.py": ("no Docker, network, or live data", "checked-in public demo artifact"),
        }
        for name, phrases in expectations.items():
            with self.subTest(script=name):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / name), "--help"],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for phrase in phrases:
                    self.assertIn(phrase, result.stdout)


if __name__ == "__main__":
    unittest.main()
