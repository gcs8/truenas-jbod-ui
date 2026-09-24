from __future__ import annotations

import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import run_compose_runtime_matrix, run_private_qa_restore, update_immutable_deployment


class ScriptPlatformGuardTests(unittest.TestCase):
    def test_immutable_updater_explains_a_missing_process_identity(self) -> None:
        fake_os = MagicMock()
        del fake_os.geteuid
        with patch.object(update_immutable_deployment, "os", fake_os):
            with self.assertRaisesRegex(update_immutable_deployment.DeploymentError, "needs a Linux Docker host"):
                update_immutable_deployment._effective_uid()

    def test_immutable_updater_refuses_before_any_action_work_begins(self) -> None:
        """The guard must run at entry, not at first ownership check.

        The uid checks are only reached while validating an existing receipt, so
        without an entry guard an update on a host with no geteuid would pull
        images, download Compose files and create the receipt directory before
        failing, leaving the marker directory behind.
        """

        invocations = {
            "update_deployment": [
                "update",
                "/srv/jbod",
                "--project-name=jbod",
                "--source-revision=" + "0" * 40,
                "--expected-image=ghcr.io/gcs8/truenas-jbod-ui@sha256:" + "0" * 64,
                "--candidate-tag=ghcr.io/gcs8/truenas-jbod-ui:v1",
                "--compose=docker-compose.yml=live.yml",
                "--service=app",
                "--health-url=http://127.0.0.1/healthz",
            ],
            "verify_deployment": ["verify", "/srv/jbod"],
            "rollback_deployment": ["rollback", "/srv/jbod"],
        }
        for entry_point, argv in invocations.items():
            with self.subTest(action=argv[0]):
                fake_os = MagicMock()
                del fake_os.geteuid
                with patch.object(update_immutable_deployment, "os", fake_os):
                    with patch.object(update_immutable_deployment, entry_point) as worker:
                        stderr = io.StringIO()
                        with contextlib.redirect_stderr(stderr):
                            code = update_immutable_deployment.main(argv)
                self.assertEqual(code, 1)
                self.assertIn("needs a Linux Docker host", stderr.getvalue())
                worker.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
