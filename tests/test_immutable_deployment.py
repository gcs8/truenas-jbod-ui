from __future__ import annotations

import email.message
import http.server
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from scripts import update_immutable_deployment as deployment


REPOSITORY = "ghcr.io/gcs8/truenas-jbod-ui"
OLD_DIGEST = f"{REPOSITORY}@sha256:{'a' * 64}"
NEW_DIGEST = f"{REPOSITORY}@sha256:{'b' * 64}"
OLD_IMAGE_ID = f"sha256:{'1' * 64}"
NEW_IMAGE_ID = f"sha256:{'2' * 64}"
CANDIDATE_TAG = f"{REPOSITORY}:v0.22.0"


class FakeRuntime:
    def __init__(
        self,
        root: Path,
        *,
        tag_digest: str = NEW_DIGEST,
        fail_activation: bool = False,
        health: str = "healthy",
        label_config_files: tuple[str, ...] | None = None,
    ) -> None:
        self.root = root
        self.tag_digest = tag_digest
        self.fail_activation = fail_activation
        self.health = health
        self.project_name = "jbod-ui"
        self.label_config_files = label_config_files or (
            str(root / "compose.yaml"),
            str(root / "docker-compose.nonroot.yml"),
        )
        self.commands: list[tuple[str, ...]] = []
        self.downloads: list[str] = []
        self.probes: list[str] = []
        self.active_image_id = OLD_IMAGE_ID
        self.restart_counts = {"enclosure-ui": 4, "enclosure-history": 2}

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> str:
        self.commands.append(tuple(command))
        if command[:2] == ["docker", "pull"]:
            return ""
        if command[:3] == ["docker", "image", "inspect"]:
            image = command[3]
            template = command[-1]
            if "RepoDigests" in template:
                if image == CANDIDATE_TAG:
                    return json.dumps([self.tag_digest])
                if image in {OLD_IMAGE_ID, OLD_DIGEST}:
                    return json.dumps([OLD_DIGEST])
                if image in {NEW_IMAGE_ID, NEW_DIGEST}:
                    return json.dumps([NEW_DIGEST])
                raise AssertionError(f"unexpected image digest lookup: {image}")
            if template == "{{.Id}}":
                if image == OLD_DIGEST:
                    return OLD_IMAGE_ID
                if image == NEW_DIGEST:
                    return NEW_IMAGE_ID
                raise AssertionError(f"unexpected image ID lookup: {image}")
        if command[:2] == ["docker", "compose"]:
            if "config" in command:
                return ""
            if command[-3:] == ["ps", "--services", "--status"]:
                raise AssertionError("status value is missing")
            if "ps" in command and "--services" in command:
                return "enclosure-ui\nenclosure-history\n"
            if "ps" in command and "-q" in command:
                service = command[-1]
                return f"container-{service}"
            if "pull" in command:
                return ""
            if "up" in command:
                image_reference = next(
                    line.split("=", 1)[1]
                    for line in (self.root / ".env").read_text(encoding="utf-8").splitlines()
                    if line.startswith("JBOD_UI_IMAGE=")
                )
                if image_reference == NEW_DIGEST and self.fail_activation:
                    self.fail_activation = False
                    raise deployment.DeploymentError("synthetic activation failure")
                self.active_image_id = NEW_IMAGE_ID if image_reference == NEW_DIGEST else OLD_IMAGE_ID
                self.restart_counts = {"enclosure-ui": 0, "enclosure-history": 0}
                return ""
        if command[:2] == ["docker", "inspect"]:
            container = command[-1]
            service = container.removeprefix("container-")
            template = command[-2]
            if template == "{{.Image}}":
                return self.active_image_id
            if template == "{{.State.Status}}":
                return "running"
            if template == "{{if .State.Health}}{{.State.Health.Status}}{{end}}":
                return self.health
            if template == "{{.RestartCount}}":
                return str(self.restart_counts[service])
            if template == '{{index .Config.Labels "com.docker.compose.project"}}':
                return self.project_name
            if template == '{{index .Config.Labels "com.docker.compose.project.working_dir"}}':
                return str(self.root)
            if template == '{{index .Config.Labels "com.docker.compose.project.config_files"}}':
                return ",".join(self.label_config_files)
        raise AssertionError(f"unexpected command: {command}")

    def download(self, url: str) -> bytes:
        self.downloads.append(url)
        source = url.rsplit("/", 1)[-1]
        return f"services:\n  # exact candidate {source}\n".encode()

    def probe(self, url: str) -> None:
        self.probes.append(url)


class ImmutableDeploymentTests(unittest.TestCase):
    def make_root(self, directory: str) -> Path:
        root = Path(directory)
        (root / ".env").write_text(
            "APP_PORT=8080\nJBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:latest\n",
            encoding="utf-8",
        )
        os.chmod(root / ".env", 0o600)
        (root / "compose.yaml").write_text("services:\n  # previous base\n", encoding="utf-8")
        (root / "docker-compose.nonroot.yml").write_text(
            "services:\n  # previous overlay\n",
            encoding="utf-8",
        )
        return root

    def make_spec(self, root: Path, *, expected_image: str = NEW_DIGEST) -> deployment.DeploymentSpec:
        return deployment.DeploymentSpec(
            root=root,
            project_name="jbod-ui",
            source_revision="c" * 40,
            expected_image=expected_image,
            candidate_tag=CANDIDATE_TAG,
            compose_files=(
                deployment.ComposeFile("docker-compose.yml", "compose.yaml"),
                deployment.ComposeFile("docker-compose.nonroot.yml", "docker-compose.nonroot.yml"),
            ),
            profiles=("history",),
            services=("enclosure-ui", "enclosure-history"),
            health_urls=("http://127.0.0.1:8080/healthz", "http://127.0.0.1:8081/healthz"),
        )

    def test_update_records_exact_contract_and_activates_verified_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)

            result = deployment.update_deployment(
                self.make_spec(root),
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )

            self.assertEqual(result["status"], "active")
            self.assertEqual(runtime.active_image_id, NEW_IMAGE_ID)
            self.assertEqual(runtime.probes, [
                "http://127.0.0.1:8080/healthz",
                "http://127.0.0.1:8081/healthz",
            ])
            self.assertIn("exact candidate docker-compose.yml", (root / "compose.yaml").read_text())
            self.assertIn("exact candidate docker-compose.nonroot.yml", (root / "docker-compose.nonroot.yml").read_text())
            self.assertIn(f"JBOD_UI_IMAGE={NEW_DIGEST}", (root / ".env").read_text())

            receipt_dir = root / deployment.RECEIPT_DIR_NAME
            self.assertEqual(stat.S_IMODE(receipt_dir.stat().st_mode), 0o700)
            deployment.validate_receipt(root)
            receipt = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["compose_files"], [
                {"source": "docker-compose.yml", "live": "compose.yaml"},
                {"source": "docker-compose.nonroot.yml", "live": "docker-compose.nonroot.yml"},
            ])
            self.assertEqual(receipt["project_name"], "jbod-ui")
            self.assertEqual(receipt["profiles"], ["history"])
            self.assertEqual(receipt["services"], ["enclosure-ui", "enclosure-history"])
            self.assertEqual(receipt["previous_image"], OLD_DIGEST)
            self.assertEqual(receipt["expected_image"], NEW_DIGEST)
            self.assertEqual(receipt["status"], "active")
            self.assertEqual(
                {row["service"]: row["restart_count"] for row in receipt["previous_services"]},
                {"enclosure-ui": 4, "enclosure-history": 2},
            )
            self.assertTrue(all(row["project_name"] == "jbod-ui" for row in receipt["previous_services"]))
            for path in receipt_dir.rglob("*"):
                expected_mode = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)

            compose_commands = [command for command in runtime.commands if command[:2] == ("docker", "compose")]
            for command in compose_commands:
                self.assertIn("--project-name", command)
                self.assertEqual(command[command.index("--project-name") + 1], "jbod-ui")
                self.assertIn("--profile", command)
                self.assertIn("history", command)
                self.assertEqual(command.count("-f"), 2)

    def test_existing_receipt_blocks_rerun_before_commands_or_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            receipt = root / deployment.RECEIPT_DIR_NAME
            receipt.mkdir(mode=0o700)
            runtime = FakeRuntime(root)

            with self.assertRaisesRegex(deployment.DeploymentError, "receipt already exists"):
                deployment.update_deployment(
                    self.make_spec(root),
                    run=runtime.run,
                    download=runtime.download,
                    probe=runtime.probe,
                )

            self.assertEqual(runtime.commands, [])
            self.assertEqual(runtime.downloads, [])

    def test_default_runner_reports_phase_and_budget_on_timeout(self) -> None:
        command = ["docker", "pull", CANDIDATE_TAG]
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(
                deployment.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(command, 600),
            ) as run:
                with self.assertRaisesRegex(
                    deployment.DeploymentError,
                    "image pull timed out after 600 seconds",
                ):
                    deployment._default_run(command, cwd=Path(temp_dir))
            self.assertEqual(run.call_args.kwargs["timeout"], 600)

    def test_health_probe_rejects_redirects_without_contacting_the_target(self) -> None:
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            redirect_target_hits = 0

            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                    return
                type(self).redirect_target_hits += 1
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/healthz"
            with mock.patch.object(deployment.time, "sleep", return_value=None):
                with self.assertRaisesRegex(deployment.DeploymentError, "health probe did not converge"):
                    deployment._default_probe(url)
            self.assertEqual(RedirectHandler.redirect_target_hits, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_health_probe_closes_rejected_redirect_responses_before_retrying(self) -> None:
        payloads: list[io.BytesIO] = []
        prior_closed: list[bool] = []

        class RedirectingOpener:
            def open(self, url: str, *, timeout: int) -> object:
                if payloads:
                    prior_closed.append(payloads[-1].closed)
                payload = io.BytesIO(b"redirect")
                payloads.append(payload)
                raise urllib.error.HTTPError(url, 302, "Found", email.message.Message(), payload)

        with (
            mock.patch.object(deployment.urllib.request, "build_opener", return_value=RedirectingOpener()),
            mock.patch.object(deployment.time, "sleep", return_value=None),
        ):
            with self.assertRaisesRegex(deployment.DeploymentError, "health probe did not converge"):
                deployment._default_probe("http://127.0.0.1:8080/healthz")
        self.assertEqual(len(payloads), 30)
        self.assertEqual(len(prior_closed), 29)
        self.assertTrue(all(prior_closed))
        self.assertTrue(all(payload.closed for payload in payloads))

    def test_candidate_tag_mismatch_fails_before_receipt_or_live_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            previous_base = (root / "compose.yaml").read_bytes()
            previous_env = (root / ".env").read_bytes()
            runtime = FakeRuntime(root, tag_digest=f"{REPOSITORY}@sha256:{'d' * 64}")

            with self.assertRaisesRegex(deployment.DeploymentError, "workflow receipt"):
                deployment.update_deployment(
                    self.make_spec(root),
                    run=runtime.run,
                    download=runtime.download,
                    probe=runtime.probe,
                )

            self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())
            self.assertEqual((root / "compose.yaml").read_bytes(), previous_base)
            self.assertEqual((root / ".env").read_bytes(), previous_env)

    def test_activation_failure_restores_previous_digest_and_compose_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            previous_base = (root / "compose.yaml").read_bytes()
            previous_overlay = (root / "docker-compose.nonroot.yml").read_bytes()
            runtime = FakeRuntime(root, fail_activation=True)

            with self.assertRaisesRegex(deployment.DeploymentError, "activation failed.*rollback completed"):
                deployment.update_deployment(
                    self.make_spec(root),
                    run=runtime.run,
                    download=runtime.download,
                    probe=runtime.probe,
                )

            self.assertEqual(runtime.active_image_id, OLD_IMAGE_ID)
            self.assertEqual((root / "compose.yaml").read_bytes(), previous_base)
            self.assertEqual((root / "docker-compose.nonroot.yml").read_bytes(), previous_overlay)
            self.assertIn(f"JBOD_UI_IMAGE={OLD_DIGEST}", (root / ".env").read_text())
            receipt = deployment.validate_receipt(root)
            self.assertEqual(receipt["status"], "rolled_back")

    def test_manual_rollback_restores_exact_previous_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            previous_base = (root / "compose.yaml").read_bytes()
            previous_overlay = (root / "docker-compose.nonroot.yml").read_bytes()
            runtime = FakeRuntime(root)
            deployment.update_deployment(
                self.make_spec(root),
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )

            result = deployment.rollback_deployment(root, run=runtime.run, probe=runtime.probe)

            self.assertEqual(result["image"], OLD_DIGEST)
            self.assertEqual(runtime.active_image_id, OLD_IMAGE_ID)
            self.assertEqual((root / "compose.yaml").read_bytes(), previous_base)
            self.assertEqual((root / "docker-compose.nonroot.yml").read_bytes(), previous_overlay)
            self.assertIn(f"JBOD_UI_IMAGE={OLD_DIGEST}", (root / ".env").read_text())
            receipt = deployment.validate_receipt(root)
            self.assertEqual(receipt["status"], "rolled_back")
            self.assertEqual(
                [row["service"] for row in receipt["result"]["services"]],
                ["enclosure-ui", "enclosure-history"],
            )

    def test_service_set_must_match_running_compose_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            spec = self.make_spec(root)
            spec = deployment.DeploymentSpec(
                **{**spec.__dict__, "services": ("enclosure-ui",)},
            )

            with self.assertRaisesRegex(deployment.DeploymentError, "running service set"):
                deployment.update_deployment(
                    spec,
                    run=runtime.run,
                    download=runtime.download,
                    probe=runtime.probe,
                )

            self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())

    def test_live_container_labels_must_match_project_and_compose_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root, label_config_files=(str(root / "compose.yaml"),))

            with self.assertRaisesRegex(deployment.DeploymentError, "container Compose chain"):
                deployment.update_deployment(
                    self.make_spec(root),
                    run=runtime.run,
                    download=runtime.download,
                    probe=runtime.probe,
                )
            self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())
            self.assertEqual(runtime.downloads, [])

    def test_receipt_mode_or_extra_file_fails_before_runtime_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            deployment.update_deployment(
                self.make_spec(root),
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )
            receipt_dir = root / deployment.RECEIPT_DIR_NAME
            (receipt_dir / "unexpected").write_text("unsafe", encoding="utf-8")
            os.chmod(receipt_dir / "unexpected", 0o600)
            command_count = len(runtime.commands)

            with self.assertRaisesRegex(deployment.DeploymentError, "receipt file set"):
                deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertEqual(len(runtime.commands), command_count)

            (receipt_dir / "unexpected").unlink()
            os.chmod(receipt_dir / "receipt.json", 0o644)
            with self.assertRaisesRegex(deployment.DeploymentError, "mode 0600"):
                deployment.rollback_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertEqual(len(runtime.commands), command_count)

    def test_verify_fails_when_an_expected_service_is_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            deployment.update_deployment(
                self.make_spec(root),
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )
            original_run = runtime.run

            def missing_service(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
                if command[:2] == ["docker", "compose"] and "ps" in command and "--services" in command:
                    return "enclosure-ui\n"
                return original_run(command, cwd=cwd, env=env)

            with self.assertRaisesRegex(deployment.DeploymentError, "running service set"):
                deployment.verify_deployment(root, run=missing_service, probe=runtime.probe)

    def test_receipt_revalidates_command_and_probe_fields_before_runtime_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            deployment.update_deployment(
                self.make_spec(root),
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )
            receipt_path = root / deployment.RECEIPT_DIR_NAME / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["services"][0] = "--project-directory"
            receipt["previous_services"][0]["service"] = "--project-directory"
            receipt["health_urls"] = ["http://192.0.2.10:8080/healthz"]
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            os.chmod(receipt_path, 0o600)
            command_count = len(runtime.commands)

            with self.assertRaisesRegex(deployment.DeploymentError, "receipt service"):
                deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertEqual(len(runtime.commands), command_count)

    def test_receipt_rejects_boolean_and_float_integer_aliases_before_runtime_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            deployment.update_deployment(
                self.make_spec(root),
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )
            receipt_path = root / deployment.RECEIPT_DIR_NAME / "receipt.json"
            original = json.loads(receipt_path.read_text(encoding="utf-8"))
            command_count = len(runtime.commands)
            cases = (
                ("boolean schema", lambda receipt: receipt.__setitem__("schema", True)),
                ("float schema", lambda receipt: receipt.__setitem__("schema", 1.0)),
                (
                    "boolean previous restart count",
                    lambda receipt: receipt["previous_services"][0].__setitem__("restart_count", True),
                ),
                (
                    "boolean result restart count",
                    lambda receipt: receipt["result"]["services"][0].__setitem__("restart_count", False),
                ),
                (
                    "float result restart count",
                    lambda receipt: receipt["result"]["services"][0].__setitem__("restart_count", 0.0),
                ),
            )
            for label, mutate in cases:
                with self.subTest(label=label):
                    receipt = json.loads(json.dumps(original))
                    mutate(receipt)
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                    os.chmod(receipt_path, 0o600)
                    with self.assertRaises(deployment.DeploymentError):
                        deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe)
                    self.assertEqual(len(runtime.commands), command_count)

    def test_malformed_repo_digest_metadata_fails_as_a_deployment_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            original_run = runtime.run

            def malformed_digests(
                command: list[str],
                *,
                cwd: Path,
                env: dict[str, str] | None = None,
            ) -> str:
                if command[:3] == ["docker", "image", "inspect"] and command[3] == CANDIDATE_TAG:
                    return "null"
                return original_run(command, cwd=cwd, env=env)

            with self.assertRaisesRegex(deployment.DeploymentError, "RepoDigests"):
                deployment.update_deployment(
                    self.make_spec(root),
                    run=malformed_digests,
                    download=runtime.download,
                    probe=runtime.probe,
                )
            self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())

    def test_unhealthy_current_service_blocks_before_receipt_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root, health="unhealthy")

            with self.assertRaisesRegex(deployment.DeploymentError, "current service health"):
                deployment.update_deployment(
                    self.make_spec(root),
                    run=runtime.run,
                    download=runtime.download,
                    probe=runtime.probe,
                )
            self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())

    def test_update_requires_at_least_one_loopback_health_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            spec = self.make_spec(root)
            spec = deployment.DeploymentSpec(
                **{**spec.__dict__, "health_urls": ()},
            )

            with self.assertRaisesRegex(deployment.DeploymentError, "at least one health URL"):
                deployment.update_deployment(
                    spec,
                    run=runtime.run,
                    download=runtime.download,
                    probe=runtime.probe,
                )
            self.assertEqual(runtime.commands, [])

    def test_receipt_rejects_unknown_result_keys_before_runtime_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            deployment.update_deployment(
                self.make_spec(root),
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )
            receipt_path = root / deployment.RECEIPT_DIR_NAME / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["result"]["unexpected"] = "value"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            os.chmod(receipt_path, 0o600)
            command_count = len(runtime.commands)

            with self.assertRaisesRegex(deployment.DeploymentError, "receipt result"):
                deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertEqual(len(runtime.commands), command_count)

    def test_runbook_uses_transactional_helper_instead_of_sourcing_receipts(self) -> None:
        runbook = Path("wiki/Docker-and-GHCR-Deployment.md").read_text(encoding="utf-8")
        self.assertIn("scripts/update_immutable_deployment.py update", runbook)
        self.assertIn("--compose docker-compose.yml=compose.yaml", runbook)
        self.assertIn("--project-name truenas-jbod-ui", runbook)
        self.assertIn("--profile history", runbook)
        self.assertIn("--service enclosure-ui", runbook)
        self.assertIn("scripts/update_immutable_deployment.py verify", runbook)
        self.assertIn("scripts/update_immutable_deployment.py rollback", runbook)
        self.assertNotIn(". ./image-update-receipt.env", runbook)
        self.assertNotIn("image-update-receipt.env", runbook)


if __name__ == "__main__":
    unittest.main()
