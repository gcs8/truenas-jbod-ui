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
from typing import Callable
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
        require_staged_env: bool = False,
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
        self.require_staged_env = require_staged_env
        self.candidate_env_bytes: bytes | None = None
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
                compose_paths = [
                    Path(command[index + 1])
                    for index, value in enumerate(command)
                    if value == "-f"
                ]
                if self.require_staged_env and any(path.parent != self.root for path in compose_paths):
                    candidate_env = compose_paths[0].parent / ".env"
                    if not candidate_env.is_file():
                        raise deployment.DeploymentError("candidate staging is missing .env")
                    self.candidate_env_bytes = candidate_env.read_bytes()
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
            spec = deployment.DeploymentSpec(**{**self.make_spec(root).__dict__, "replace_compose": True})

            result = deployment.update_deployment(
                spec,
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

    def test_candidate_compose_preflight_stages_private_live_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            expected_env = (root / ".env").read_bytes()
            runtime = FakeRuntime(root, require_staged_env=True)
            spec = deployment.DeploymentSpec(**{**self.make_spec(root).__dict__, "replace_compose": True})

            result = deployment.update_deployment(
                spec,
                run=runtime.run,
                download=runtime.download,
                probe=runtime.probe,
            )

            self.assertEqual(result["status"], "active")
            self.assertEqual(runtime.candidate_env_bytes, expected_env)

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

    def test_a_failed_health_probe_does_not_leak_its_url_into_the_rollback_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            secret_url = "http://127.0.0.1:8081/healthz?token=SYNTH-SECRET"
            spec = deployment.DeploymentSpec(**{**self.make_spec(root).__dict__, "health_urls": (secret_url,)})
            probes: list[str] = []

            def probe(url: str) -> None:
                probes.append(url)
                # The candidate's health never converges; the restored one does.
                if len(probes) == 1:
                    raise deployment.DeploymentError(f"health probe did not converge: {url}")

            with self.assertRaises(deployment.DeploymentError) as caught:
                deployment.update_deployment(spec, run=runtime.run, download=runtime.download, probe=probe)

            message = str(caught.exception)
            self.assertEqual(
                message,
                "activation failed (health probe did not converge: [redacted-url]); "
                "automatic rollback completed",
            )
            self.assertNotIn("SYNTH-SECRET", message)
            self.assertNotIn("/healthz", message)

    def test_failure_summary_names_helper_errors_and_only_types_otherwise(self) -> None:
        self.assertEqual(
            deployment._failure_summary(deployment.DeploymentError("container  enclosure-history\nis unhealthy")),
            "container enclosure-history is unhealthy",
        )
        self.assertEqual(len(deployment._failure_summary(deployment.DeploymentError("x" * 1000))), 300)
        # No part of a URL can be trusted as a host in an error summary.
        self.assertEqual(
            deployment._failure_summary(deployment.DeploymentError(
                "health probe did not converge: http://user:pw@127.0.0.1:8081/healthz?token=SYNTH-SECRET#frag"
            )),
            "health probe did not converge: [redacted-url]",
        )
        # Arbitrary exceptions can carry paths or payloads; only the type is shown.
        self.assertEqual(deployment._failure_summary(ValueError("/secret/path token=abc")), "ValueError")
        self.assertEqual(deployment._failure_summary(KeyboardInterrupt()), "KeyboardInterrupt")

    def test_failure_summary_redacts_entire_url_tokens_without_parsing_authorities(self) -> None:
        urls = (
            "https://SYNTH-USER:SYNTH-PA/SS@host.example.test/x",
            "https://SYNTH-USER:SYNTH-PA?SS@host.example.test/x",
            "https://SYNTH-USER:SYNTH-PA#SS@host.example.test/x",
            "https://SYNTH-USER:SYNTH%2FPA%40SS@host.example.test/x",
            "https://SYNTH-USER%3ASYNTH-PASS%40host.example.test/x",
            "https://SYNTH-USER:SYNTH-PASS",
            "https://[invalid/SYNTH-PASS",
            "https:///SYNTH-PASS?token=SYNTH-TOKEN#SYNTH-FRAGMENT",
            "https://host.example.test/SYNTH-PASS?token=SYNTH-TOKEN#SYNTH-FRAGMENT",
            "https%3A%2F%2FSYNTH-USER%3ASYNTH-PASS%40host.example.test/x",
            "HTTPS:/SYNTH-USER:SYNTH-PASS@host.example.test",
            "https:SYNTH-PASS",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(
                    deployment._failure_summary(deployment.DeploymentError(f"failed: {url} next step")),
                    "failed: [redacted-url] next step",
                )

    def test_failure_summary_redacts_multiple_urls_and_preserves_non_url_text(self) -> None:
        self.assertEqual(
            deployment._failure_summary(deployment.DeploymentError(
                "failed https://SYNTH-USER:SYNTH-PASS@host.example.test/x then "
                "ssh://SYNTH-USER:SYNTH-PA/SS@other.example.test/x exit=2"
            )),
            "failed [redacted-url] then [redacted-url] exit=2",
        )
        self.assertEqual(
            deployment._failure_summary(deployment.DeploymentError("count:33 /local/path retry=5")),
            "count:33 /local/path retry=5",
        )

    def test_retention_scope_too_large_http_body_is_named_without_retrying(self) -> None:
        body = io.BytesIO(json.dumps({
            "error": "retention_scope_too_large", "enclosure_count_minimum": 33,
            "enclosure_cap": 32, "detail": "SYNTH-PRIVATE-DETAIL",
        }).encode())
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8080/api/inventory", 503, "Unavailable",
            email.message.Message(), body,
        )
        with (
            mock.patch.object(deployment.urllib.request, "build_opener") as opener,
            mock.patch.object(deployment.time, "sleep") as sleep,
        ):
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(
                deployment.DeploymentError,
                "retention scope too large: at least 33 enclosure options exceeds cap 32",
            ) as caught:
                deployment._retention_totals(deployment._default_fetch_json, error.url)
            sleep.assert_not_called()
            opener.return_value.open.assert_called_once()
        self.assertTrue(body.closed)
        self.assertNotIn("SYNTH-PRIVATE-DETAIL", str(caught.exception))
        self.assertNotIsInstance(caught.exception, deployment.InventoryBusy)

    def test_legacy_over_cap_http_body_is_named_without_retrying(self) -> None:
        body = io.BytesIO(json.dumps({
            "ok": False,
            "detail": "This system has too many enclosures for a system-wide disk check.",
        }).encode())
        read = mock.Mock(wraps=body.read)
        body.read = read
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8080/api/inventory", 503, "Unavailable",
            email.message.Message(), body,
        )
        with (
            mock.patch.object(deployment.urllib.request, "build_opener") as opener,
            mock.patch.object(deployment.time, "sleep") as sleep,
        ):
            opener.return_value.open.side_effect = error
            with self.assertRaises(deployment.RetentionScopeTooLarge) as caught:
                deployment._retention_totals(deployment._default_fetch_json, error.url)
            self.assertEqual(str(caught.exception), "inventory retention scope too large")
            opener.return_value.open.assert_called_once()
            sleep.assert_not_called()
        read.assert_called_once_with(4097)
        self.assertTrue(body.closed)

    def test_legacy_over_cap_recognition_requires_the_exact_response(self) -> None:
        legacy = {
            "ok": False,
            "detail": "This system has too many enclosures for a system-wide disk check.",
        }
        cases = (
            (503, {"ok": False, "detail": "Unrelated legacy failure: SYNTH-PRIVATE-DETAIL"}),
            (503, {**legacy, "detail": legacy["detail"] + " SYNTH-PRIVATE-DETAIL"}),
            (503, {**legacy, "detail": "Prefix " + legacy["detail"]}),
            (503, {**legacy, "detail": legacy["detail"].lower()}),
            (503, {**legacy, "ok": True}),
            (503, {**legacy, "ok": 0}),
            (503, {"detail": legacy["detail"]}),
            (503, {**legacy, "error": "unrelated_error"}),
            (500, legacy),
            (503, json.dumps(legacy).encode() + b" " * 4096),
        )
        for code, payload in cases:
            with self.subTest(code=code, payload=payload):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                body = io.BytesIO(raw)
                error = urllib.error.HTTPError(
                    "http://127.0.0.1:8080/api/inventory", code, "Unavailable",
                    email.message.Message(), body,
                )
                with (
                    mock.patch.object(deployment.urllib.request, "build_opener") as opener,
                    mock.patch.object(deployment.time, "sleep") as sleep,
                ):
                    opener.return_value.open.side_effect = error
                    with self.assertRaises(deployment.DeploymentError) as caught:
                        deployment._retention_totals(deployment._default_fetch_json, error.url)
                    self.assertNotIsInstance(caught.exception, deployment.RetentionScopeTooLarge)
                    self.assertEqual(str(caught.exception), "inventory retention check could not read the inventory")
                    opener.return_value.open.assert_called_once()
                    sleep.assert_not_called()
                self.assertTrue(body.closed)

    def test_first_upgrade_reads_legacy_predecessor_then_typed_candidate(self) -> None:
        totals = {"summary": {key: 0 for key in deployment.RETENTION_KEYS}}
        for baseline_over_cap in (True, False):
            with self.subTest(baseline_over_cap=baseline_over_cap), tempfile.TemporaryDirectory() as directory:
                root = self.make_root(directory)
                previous = {name: (root / name).read_bytes() for name in (
                    ".env", "compose.yaml", "docker-compose.nonroot.yml",
                )}
                runtime = FakeRuntime(root)
                spec = deployment.DeploymentSpec(**{
                    **self.make_spec(root).__dict__,
                    "inventory_url": "http://127.0.0.1:8080/api/inventory",
                })
                requested_images: list[str] = []
                bodies: list[io.BytesIO] = []

                def open_inventory(url: str, *, timeout: int) -> object:
                    requested_images.append(runtime.active_image_id)
                    if runtime.active_image_id == OLD_IMAGE_ID and not baseline_over_cap:
                        response = mock.MagicMock()
                        response.__enter__.return_value = response
                        response.geturl.return_value = url
                        response.status = 200
                        response.read.return_value = json.dumps(totals).encode()
                        return response
                    # The predecessor does not yet publish the candidate's typed contract.
                    payload = {
                        "ok": False,
                        "detail": "This system has too many enclosures for a system-wide disk check.",
                    } if runtime.active_image_id == OLD_IMAGE_ID else {
                        "ok": False, "error": "retention_scope_too_large",
                        "enclosure_count_minimum": 33, "enclosure_cap": 32,
                        "detail": "SYNTH-PRIVATE-CANDIDATE-DETAIL",
                    }
                    body = io.BytesIO(json.dumps(payload).encode())
                    bodies.append(body)
                    raise urllib.error.HTTPError(url, 503, "Unavailable", email.message.Message(), body)

                with (
                    mock.patch.object(deployment.urllib.request, "build_opener") as opener,
                    mock.patch.object(deployment.time, "sleep") as sleep,
                ):
                    opener.return_value.open.side_effect = open_inventory
                    with self.assertRaisesRegex(deployment.DeploymentError, "retention scope too large") as caught:
                        deployment.update_deployment(
                            spec, run=runtime.run, probe=runtime.probe,
                            fetch_json=deployment._default_fetch_json,
                        )
                    sleep.assert_not_called()
                self.assertTrue(all(body.closed for body in bodies))
                self.assertEqual(runtime.active_image_id, OLD_IMAGE_ID)
                self.assertNotIn("SYNTH-PRIVATE", str(caught.exception))
                for name in ("compose.yaml", "docker-compose.nonroot.yml"):
                    self.assertEqual((root / name).read_bytes(), previous[name])
                if baseline_over_cap:
                    self.assertEqual(requested_images, [OLD_IMAGE_ID])
                    self.assertEqual((root / ".env").read_bytes(), previous[".env"])
                    self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())
                    self.assertFalse(any(command[:2] == ("docker", "pull") for command in runtime.commands))
                    self.assertFalse(any(command[:2] == ("docker", "compose") and "up" in command
                                         for command in runtime.commands))
                else:
                    self.assertEqual(requested_images, [OLD_IMAGE_ID, NEW_IMAGE_ID])
                    self.assertIn("at least 33 enclosure options exceeds cap 32", str(caught.exception))
                    self.assertIn("automatic rollback completed", str(caught.exception))
                    self.assertEqual(deployment.validate_receipt(root)["status"], "rolled_back")

    def test_retention_error_body_is_bounded_and_never_echoed(self) -> None:
        typed = {"error": "retention_scope_too_large", "detail": "SYNTH-PRIVATE-DETAIL"}
        cases = (
            (json.dumps({**typed, "enclosure_count_minimum": True, "enclosure_cap": 32}).encode(), True),
            (json.dumps({**typed, "enclosure_count_minimum": 33, "enclosure_cap": "SYNTH-PRIVATE"}).encode(), True),
            (json.dumps({**typed, "enclosure_count_minimum": 32, "enclosure_cap": 33}).encode(), True),
            (json.dumps({**typed, "enclosure_count_minimum": 2**64, "enclosure_cap": 32}).encode(), True),
            (b'{"error": "retention_scope_too_large"', False),
            (b'\xff', False),
            (b'[]', False),
            (b'[' * 1500 + b']' * 1500, False),
            (b'{"detail": "SYNTH-PRIVATE-DETAIL"}', False),
            (json.dumps(typed).encode() + b' ' * deployment.MAX_RETENTION_ERROR_BYTES, False),
        )
        for raw, is_typed in cases:
            with self.subTest(raw=raw[:60]):
                body = io.BytesIO(raw)
                read = mock.Mock(wraps=body.read)
                body.read = read
                error = urllib.error.HTTPError(
                    "http://127.0.0.1:8080/api/inventory", 503, "Unavailable",
                    email.message.Message(), body,
                )
                with mock.patch.object(deployment.urllib.request, "build_opener") as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaises(deployment.DeploymentError) as caught:
                        deployment._default_fetch_json(error.url)
                expected = "inventory retention scope too large" if is_typed else (
                    "inventory retention check could not read the inventory"
                )
                self.assertEqual(str(caught.exception), expected)
                self.assertTrue(body.closed)
                read.assert_called_once_with(deployment.MAX_RETENTION_ERROR_BYTES + 1)

    def test_numeric_retry_after_keeps_precedence_and_does_not_read_error_body(self) -> None:
        cases = (
            (retry_after, raw)
            for retry_after in ("0", "5", " 30 ")
            for raw in (
                b'{"error": "retention_scope_too_large"}',
                b'{"ok": false, "detail": "This system has too many enclosures for a system-wide disk check."}',
            )
        )
        for retry_after, raw in cases:
            with self.subTest(retry_after=retry_after, raw=raw):
                body = io.BytesIO(raw)
                read = mock.Mock(wraps=body.read)
                body.read = read
                headers = email.message.Message()
                headers["Retry-After"] = retry_after
                error = urllib.error.HTTPError(
                    "http://127.0.0.1:8080/api/inventory", 503, "Unavailable", headers, body,
                )
                with mock.patch.object(deployment.urllib.request, "build_opener") as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaises(deployment.InventoryBusy) as caught:
                        deployment._default_fetch_json(error.url)
                self.assertEqual(caught.exception.retry_after_seconds, int(retry_after))
                read.assert_not_called()
                self.assertTrue(body.closed)

    def test_over_cap_before_refuses_and_after_rolls_back_without_bypass(self) -> None:
        totals = {"summary": {key: 0 for key in deployment.RETENTION_KEYS}}
        for baseline_over_cap in (True, False):
            with self.subTest(baseline_over_cap=baseline_over_cap), tempfile.TemporaryDirectory() as directory:
                root = self.make_root(directory)
                previous = (root / ".env").read_bytes()
                runtime = FakeRuntime(root)
                spec = deployment.DeploymentSpec(**{
                    **self.make_spec(root).__dict__,
                    "inventory_url": "http://127.0.0.1:8080/api/inventory",
                })
                over_cap = deployment.RetentionScopeTooLarge({
                    "enclosure_count_minimum": 33, "enclosure_cap": 32,
                })
                answers = [over_cap] if baseline_over_cap else [totals, over_cap]

                def fetch(url: str) -> object:
                    answer = answers.pop(0)
                    if isinstance(answer, Exception):
                        raise answer
                    return answer

                with self.assertRaisesRegex(deployment.DeploymentError, "retention scope too large") as caught:
                    deployment.update_deployment(
                        spec, run=runtime.run, probe=runtime.probe, fetch_json=fetch,
                    )
                self.assertEqual(runtime.active_image_id, OLD_IMAGE_ID)
                if baseline_over_cap:
                    self.assertEqual((root / ".env").read_bytes(), previous)
                    self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())
                    self.assertFalse(any(command[:2] == ("docker", "pull") for command in runtime.commands))
                else:
                    self.assertIn("automatic rollback completed", str(caught.exception))
                    self.assertEqual(deployment.validate_receipt(root)["status"], "rolled_back")

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

            # #910: the message names the cause, not just "activation failed".
            with self.assertRaisesRegex(
                deployment.DeploymentError,
                r"^activation failed \(synthetic activation failure\); automatic rollback completed$",
            ):
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

    HISTORY_HEALTH_URL = "http://127.0.0.1:8081/healthz"

    @staticmethod
    def history_health(*, recovery_required: object = False, paused: object = False,
                       detail: str | None = None) -> dict[str, object]:
        """A synthetic history /healthz body: HTTP 200 for ok and degraded alike."""
        degraded = bool(detail) or recovery_required is True or paused is True
        return {
            "status": "degraded" if degraded else "ok",
            "detail": detail,
            "collector": {
                "history_recovery_required": recovery_required,
                "history_quarantined_at": "2026-01-02T03:04:05Z" if recovery_required is True else None,
                "history_collection_paused": paused,
                "history_collection_paused_at": "2026-01-02T03:04:06Z" if paused is True else None,
                "last_error": "History collector error; see service logs." if detail else None,
            },
            "database_size_bytes": 4096,
        }

    def durable_update(
        self, root: Path, answers: list[object],
    ) -> tuple[FakeRuntime, Callable[[], dict[str, object]], list[str]]:
        runtime = FakeRuntime(root)
        spec = deployment.DeploymentSpec(
            **{**self.make_spec(root).__dict__, "history_health_url": self.HISTORY_HEALTH_URL},
        )
        reads: list[str] = []

        def fetch_health(url: str) -> object:
            reads.append(url)
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        def update() -> dict[str, object]:
            return deployment.update_deployment(
                spec, run=runtime.run, download=runtime.download, probe=runtime.probe,
                fetch_health=fetch_health,
            )

        runtime.fetch_health = fetch_health  # type: ignore[attr-defined]
        return runtime, update, reads

    def assert_durable_non_pass_without_rollback(self, answers: list[object], expected: dict[str, object]) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime, update, reads = self.durable_update(root, answers)

            with self.assertRaises(deployment.DurableStateNotAccepted) as caught:
                update()

            record = {"history_health_url": self.HISTORY_HEALTH_URL, **expected}
            # Image convergence is real and stays recorded; durable acceptance is separate.
            self.assertEqual(caught.exception.result["status"], "active")
            self.assertEqual(caught.exception.result["durable_state"], record)
            self.assertIn(str(expected["state"]), str(caught.exception))
            self.assertIn("not rolled back", str(caught.exception))
            receipt = deployment.validate_receipt(root)
            self.assertEqual(receipt["status"], "active")
            self.assertEqual(receipt["durable_state"], record)
            # No automatic rollback, restart or repair: one activation only.
            self.assertEqual(runtime.active_image_id, NEW_IMAGE_ID)
            self.assertEqual(sum(1 for command in runtime.commands if "up" in command), 1)
            self.assertIn(f"JBOD_UI_IMAGE={NEW_DIGEST}", (root / ".env").read_text())
            self.assertEqual(reads, [self.HISTORY_HEALTH_URL])
            receipt_text = (root / deployment.RECEIPT_DIR_NAME / "receipt.json").read_text(encoding="utf-8")
            self.assertNotIn("2026-01-02", receipt_text)
            self.assertNotIn("SYNTH", receipt_text)

            # verify re-reads the same durable state and keeps reporting the non-pass.
            answers.append(self.history_health(
                recovery_required=expected["history_recovery_required"],
                paused=expected["history_collection_paused"],
            ) if expected["state"] != "unknown" else None)
            with self.assertRaises(deployment.DurableStateNotAccepted) as verified:
                deployment.verify_deployment(
                    root, run=runtime.run, probe=runtime.probe,
                    fetch_health=runtime.fetch_health,  # type: ignore[attr-defined]
                )
            self.assertEqual(verified.exception.result["image"], NEW_DIGEST)
            self.assertEqual(verified.exception.result["durable_state"]["state"], expected["state"])

            # A deliberate manual rollback stays available, does not read or
            # claim to repair durable state, and keeps the activation evidence.
            reads.clear()
            deployment.rollback_deployment(root, run=runtime.run, probe=runtime.probe)
            receipt = deployment.validate_receipt(root)
            self.assertEqual(receipt["status"], "rolled_back")
            self.assertEqual(receipt["durable_state"], record)
            self.assertEqual(reads, [])

    def test_durable_state_accepts_normal_history_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime, update, reads = self.durable_update(root, [self.history_health(), self.history_health()])

            result = update()

            record = {
                "history_health_url": self.HISTORY_HEALTH_URL,
                "state": "intact",
                "history_recovery_required": False,
                "history_collection_paused": False,
            }
            self.assertEqual(result["status"], "active")
            self.assertEqual(result["durable_state"], record)
            self.assertEqual(deployment.validate_receipt(root)["durable_state"], record)
            self.assertEqual(reads, [self.HISTORY_HEALTH_URL])
            verified = deployment.verify_deployment(
                root, run=runtime.run, probe=runtime.probe,
                fetch_health=runtime.fetch_health,  # type: ignore[attr-defined]
            )
            self.assertEqual(verified["durable_state"], record)

    def test_durable_state_accepts_benign_appliance_degradation(self) -> None:
        # A failed collection pass against an unreachable appliance is degraded
        # (HTTP 200) but says nothing about the local store; it must still pass.
        degraded = self.history_health(detail="SYNTH appliance unreachable")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime, update, _ = self.durable_update(root, [degraded])

            result = update()

            self.assertEqual(result["durable_state"]["state"], "intact")
            self.assertEqual(runtime.active_image_id, NEW_IMAGE_ID)
            receipt_text = (root / deployment.RECEIPT_DIR_NAME / "receipt.json").read_text(encoding="utf-8")
            self.assertNotIn("SYNTH", receipt_text)
            self.assertNotIn("degraded", receipt_text)

    def test_recovery_required_history_is_a_distinct_non_pass(self) -> None:
        self.assert_durable_non_pass_without_rollback(
            [self.history_health(recovery_required=True)],
            {"state": "recovery_required", "history_recovery_required": True,
             "history_collection_paused": False},
        )

    def test_paused_history_collection_is_a_distinct_non_pass(self) -> None:
        self.assert_durable_non_pass_without_rollback(
            [self.history_health(paused=True)],
            {"state": "collection_paused", "history_recovery_required": False,
             "history_collection_paused": True},
        )

    def test_unreadable_durable_state_is_unknown_not_healthy(self) -> None:
        for label, answer in (
            ("read failure", deployment.DeploymentError("history health could not be read")),
            ("no collector", {"status": "ok", "detail": None}),
            ("string flag", self.history_health(recovery_required="false")),
            ("integer flag", self.history_health(paused=0)),
            ("not an object", ["ok"]),
        ):
            with self.subTest(label=label):
                recovery = None if label in {"read failure", "no collector", "not an object", "string flag"} else False
                paused = None if label != "string flag" else False
                self.assert_durable_non_pass_without_rollback(
                    [answer],
                    {"state": "unknown", "history_recovery_required": recovery,
                     "history_collection_paused": paused},
                )

    def test_durable_state_is_opt_in_and_absent_receipts_stay_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)

            def unexpected(url: str) -> object:
                raise AssertionError("durable state must not be read without --history-health-url")

            result = deployment.update_deployment(
                self.make_spec(root), run=runtime.run, download=runtime.download,
                probe=runtime.probe, fetch_health=unexpected,
            )
            self.assertNotIn("durable_state", result)
            self.assertNotIn("durable_state", deployment.validate_receipt(root))
            self.assertNotIn(
                "durable_state",
                deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe, fetch_health=unexpected),
            )

    def test_history_health_url_must_be_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime = FakeRuntime(root)
            spec = deployment.DeploymentSpec(
                **{**self.make_spec(root).__dict__, "history_health_url": "http://192.0.2.10:8081/healthz"},
            )
            with self.assertRaisesRegex(deployment.DeploymentError, "history health URL must use an explicit loopback"):
                deployment.update_deployment(spec, run=runtime.run)
            self.assertEqual(runtime.commands, [])

    def test_receipt_durable_state_record_is_revalidated_before_runtime_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_root(temp_dir)
            runtime, update, _ = self.durable_update(root, [self.history_health()])
            update()
            receipt_path = root / deployment.RECEIPT_DIR_NAME / "receipt.json"
            original = json.loads(receipt_path.read_text(encoding="utf-8"))
            command_count = len(runtime.commands)
            cases = (
                ("remote URL", lambda record: record.__setitem__("history_health_url", "http://192.0.2.10:8081/healthz")),
                ("unknown state", lambda record: record.__setitem__("state", "repaired")),
                ("integer flag", lambda record: record.__setitem__("history_collection_paused", 0)),
                ("extra key", lambda record: record.__setitem__("detail", "SYNTH")),
                ("missing key", lambda record: record.pop("history_recovery_required")),
                ("intact while paused", lambda record: record.__setitem__("history_collection_paused", True)),
                ("unobserved with flags", lambda record: record.__setitem__("state", None)),
            )
            for label, mutate in cases:
                with self.subTest(label=label):
                    receipt = json.loads(json.dumps(original))
                    mutate(receipt["durable_state"])
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                    os.chmod(receipt_path, 0o600)
                    with self.assertRaisesRegex(deployment.DeploymentError, "durable"):
                        deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe)
                    self.assertEqual(len(runtime.commands), command_count)

    def test_default_history_health_reader_is_bounded_loopback_json_only(self) -> None:
        body = json.dumps(self.history_health(recovery_required=True)).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/healthz")
                    self.end_headers()
                    return
                payload = {
                    "/healthz": body,
                    "/down": b'{"status": "down"}',
                    "/text": b"not json",
                    "/huge": b" " * (deployment.MAX_HISTORY_HEALTH_BYTES + 1),
                }[self.path]
                self.send_response(503 if self.path == "/down" else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(deployment._default_fetch_history_health(f"{base}/healthz"), json.loads(body))
            for path in ("/redirect", "/down", "/text", "/huge"):
                with self.subTest(path=path):
                    with self.assertRaises(deployment.DeploymentError):
                        deployment._default_fetch_history_health(f"{base}{path}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cli_forwards_history_health_url_and_exits_three_on_durable_non_pass(self) -> None:
        argv = [
            "update", "/srv/jbod", "--project-name=jbod", "--source-revision=" + "0" * 40,
            "--expected-image=" + NEW_DIGEST, "--candidate-tag=" + CANDIDATE_TAG,
            "--compose=docker-compose.yml=live.yml", "--service=app",
            "--health-url=http://127.0.0.1:8081/healthz",
            "--history-health-url=" + self.HISTORY_HEALTH_URL,
        ]
        self.assertEqual(deployment._build_parser().parse_args(argv).history_health_url, self.HISTORY_HEALTH_URL)
        result = {
            "status": "active", "image": NEW_DIGEST,
            "durable_state": {"history_health_url": self.HISTORY_HEALTH_URL, "state": "recovery_required",
                              "history_recovery_required": True, "history_collection_paused": False},
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(deployment, "_require_linux_docker_host"),
            mock.patch.object(deployment, "update_deployment",
                              side_effect=deployment.DurableStateNotAccepted(result)) as update,
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            code = deployment.main(argv)
        self.assertEqual(code, 3)
        self.assertEqual(update.call_args.args[0].history_health_url, self.HISTORY_HEALTH_URL)
        self.assertEqual(json.loads(stdout.getvalue()), result)
        self.assertIn("recovery_required", stderr.getvalue())

    def test_runbook_documents_opt_in_durable_state_acceptance(self) -> None:
        runbook = " ".join(Path("docs/IMMUTABLE_GHCR_DEPLOYMENT.md").read_text(encoding="utf-8").split())
        for text in ("--history-health-url http://127.0.0.1:8081/healthz", "history_recovery_required",
                     "history_collection_paused", "exits with status 3", "does not roll back"):
            self.assertIn(text, runbook)

    def test_runbook_uses_transactional_helper_instead_of_sourcing_receipts(self) -> None:
        runbook = Path("docs/IMMUTABLE_GHCR_DEPLOYMENT.md").read_text(encoding="utf-8")
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
