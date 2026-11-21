"""Synthetic producer, real inventory/routes/store, and matrix HTTP diagnostics."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_compose_runtime_matrix.py"
FIXTURE = ROOT / "tests/fixtures/ci-smoke-config.yaml"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(mode, directory):
    """Fresh process: no inherited application singletons or configured services."""
    from websockets.sync.server import serve

    matrix = load_file("matrix", SCRIPT)
    root = Path(directory) / "variant"

    def local_install(command, **kwargs):
        # Replace only privileged filesystem transport, not config generation.
        command = list(command)
        if command[:3] == ["sudo", "install", "-d"]:
            for path in command[command.index("-g") + 2:]:
                Path(path).mkdir(parents=True, exist_ok=True)
        elif command[:2] == ["sudo", "install"]:
            shutil.copyfile(command[-2], command[-1])
        else:
            raise AssertionError("unexpected preparation command")

    with patch.object(matrix, "_run", side_effect=local_install):
        matrix._prepare_variant_root(
            root, variant=matrix.VARIANTS[0], compose_path=ROOT / "docker-compose.yml",
            config_fixture=FIXTURE, image="sha256:" + "a" * 64,
            ports=matrix.Ports(19080, 19081, 19082),
        )
    if mode == "unread":
        shutil.copyfile(FIXTURE, root / "config/config.yaml")

    with contextlib.ExitStack() as stack:
        producer_path = root / "config/compose-runtime-source.py"
        producer = None
        if mode == "live" and producer_path.exists():
            producer = load_file("producer", producer_path)
            server = stack.enter_context(serve(producer.handle, "127.0.0.1", 0))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            stack.callback(thread.join, 5)
            stack.callback(server.shutdown)
            # The runtime producer uses a container-private fixed port. Use the
            # same handler with an OS-assigned port for concurrent native tests.
            config = root / "config/config.yaml"
            config.write_text(config.read_text().replace(":19443", f":{server.socket.getsockname()[1]}"))

        os.environ.update({
            "APP_CONFIG_PATH": str(root / "config/config.yaml"),
            "APP_SERVICE_ROLE": "ui", "RELEASE_CHECK_ENABLED": "false",
            "READ_UI_AUTH_MODE": "basic", "READ_UI_AUTH_USERNAME": matrix.AUTH_USERNAME,
            "READ_UI_AUTH_PASSWORD": matrix.AUTH_PASSWORD,
            "ADMIN_AUTH_MODE": "basic", "ADMIN_AUTH_USERNAME": matrix.AUTH_USERNAME,
            "ADMIN_AUTH_PASSWORD": matrix.AUTH_PASSWORD,
            "APP_PUBLIC_ORIGIN": "http://127.0.0.1:19080",
            "BACKUP_TARGETS_JSON": "[]", "BACKUP_CONFIG_ENABLED": "false",
            "BACKUP_FULL_ENABLED": "false", "SCHEDULED_BACKUP_ENABLED": "false",
            "BACKUP_JOURNAL_PATH": str(root / "journal/changes.jsonl"),
        })
        from app.config import get_settings
        from app.main import create_app
        from app import route_support

        settings = get_settings()
        for name in ("mapping_file", "slot_detail_cache_file", "log_file"):
            assert Path(getattr(settings.paths, name)).resolve().is_relative_to(root)
        assert all(not system.ssh.enabled for system in settings.systems)
        application = create_app()
        requests = []
        restarts = []
        snapshots = []
        path = root / "data/slot_mappings.json"

        def asgi_request(url, *, method="GET", payload=None, authenticated=False, origin=None):
            parts = urllib.parse.urlsplit(url)
            headers = [(b"host", b"127.0.0.1:19080"), (b"content-type", b"application/json")]
            if authenticated:
                headers.append((b"authorization", matrix._authorization_header().encode()))
            if origin:
                headers.append((b"origin", origin.encode()))
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "scheme": "http", "method": method, "path": parts.path, "raw_path": parts.path.encode(),
                     "query_string": parts.query.encode(), "headers": headers,
                     "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 19080), "root_path": ""}
            sent = []
            pending = [{"type": "http.request", "body": b"" if payload is None else json.dumps(payload).encode(), "more_body": False}]

            async def invoke():
                completed = asyncio.Event()

                async def receive():
                    if pending:
                        return pending.pop()
                    await completed.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    sent.append(message)
                    if message["type"] == "http.response.body" and not message.get("more_body", False):
                        completed.set()

                await asyncio.wait_for(application(scope, receive, send), 15)

            asyncio.run(invoke())
            status = next(item["status"] for item in sent if item["type"] == "http.response.start")
            body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            requests.append({"method": method, "path": parts.path, "status": status,
                             "payload": payload, "body": json.loads(body)})
            if parts.path == "/api/inventory":
                snapshots.append(json.loads(body))
            return status, body

        def restart(*args):
            nonlocal application
            restarts.append(json.loads(path.read_text()))
            # Discard the real service/store objects, as a UI process restart does.
            from app import main as app_main
            from app.settings_reload import SettingsRuntime

            previous = route_support.get_inventory_registry()
            runtime = SettingsRuntime(get_settings)
            app_main.SETTINGS_RUNTIME = route_support.SETTINGS_RUNTIME = runtime
            application = create_app()
            assert route_support.get_inventory_registry() is not previous
            status, body = asgi_request(
                "http://127.0.0.1:19080/api/inventory?system_id=matrix-live&enclosure_id=matrix-shelf",
                authenticated=True,
            )
            assert status == 200
            assert json.loads(body)["slots"][0]["notes"] == "Matrix ui-only"

        error = None
        with (
            patch.object(matrix, "_request", side_effect=asgi_request),
            patch.object(matrix, "_restart_ui", side_effect=restart),
            patch.object(matrix, "_read_app_owned_json", side_effect=lambda p: json.loads(p.read_bytes())),
            patch.object(matrix, "_read_app_owned_metadata", side_effect=lambda p: (os.getuid(), os.getgid(), p.stat().st_mode, p.stat().st_size)),
        ):
            try:
                matrix._verify_mapping_cycle(root, matrix.VARIANTS[0], matrix.Ports(19080, 19081, 19082), (),
                                             expected_owner=(os.getuid(), os.getgid()))
            except RuntimeError as exc:
                error = str(exc)
            if mode == "unread":
                asgi_request("http://127.0.0.1:19080/api/inventory", authenticated=True)
        return {"error": error, "requests": requests, "restarts": restarts, "snapshots": snapshots,
                "store": json.loads(path.read_bytes()) if path.exists() else None,
                "producer_calls": producer.CALLS if producer is not None else []}


class ComposeRuntimeOccupancyTests(unittest.TestCase):
    def run_probe(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"PATH": os.defpath, "HOME": directory, "TMPDIR": directory,
                           "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", mode, directory],
                                    cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_smoke_fixture_matrix_request_refuses_unread_bay_without_mutation(self):
        result = self.run_probe("unread")
        post = next(item for item in result["requests"] if item["method"] == "POST")
        self.assertEqual(post["status"], 409)
        self.assertEqual(post["body"]["error"], "mapping_disk_unverifiable")
        self.assertEqual(post["body"]["current_revision"], post["payload"]["expected_revision"])
        self.assertEqual(post["payload"]["notes"], "Matrix ui-only")
        self.assertIs(post["payload"]["clear_identify_after_save"], False)
        self.assertEqual(result["snapshots"][0]["slots"][0]["mapping_revision"],
                         result["snapshots"][-1]["slots"][0]["mapping_revision"])
        self.assertIs(result["snapshots"][0]["slots"][0]["presence_known"], False)
        self.assertIsNone(result["store"])
        self.assertEqual(result["restarts"], [])

    def test_prepared_live_producer_runs_real_mapping_persistence_restart_and_clear(self):
        result = self.run_probe("live")
        self.assertIsNone(result["error"], result["error"])
        self.assertEqual(result["snapshots"][0]["slots"][0]["state"], "empty")
        self.assertIsNot(result["snapshots"][0]["slots"][0].get("presence_known"), False)
        mutations = [item for item in result["requests"] if item["method"] in {"POST", "DELETE"}]
        self.assertEqual([item["status"] for item in mutations], [409, 200, 200])
        self.assertEqual(mutations[0]["body"]["error"], "mapping_disk_unverifiable")
        self.assertEqual(len(result["restarts"]), 1)
        mapping = next(iter(result["restarts"][0]["slot_mappings"].values()))
        self.assertEqual(mapping["notes"], "Matrix ui-only")
        self.assertEqual(mapping["slot"], 0)
        self.assertEqual(result["store"]["slot_mappings"], {})
        self.assertGreaterEqual(result["producer_calls"].count("enclosure.query"), 3)
        self.assertIn("disk.query", result["producer_calls"])


class ComposeRuntimeSourceTests(unittest.TestCase):
    def setUp(self):
        self.matrix = load_file("matrix", SCRIPT)
        self.source = load_file("source", ROOT / "tests/fixtures/compose-runtime-source.py")

    def test_producer_refuses_unauthed_queries_and_all_mutations(self):
        class Connection:
            def __init__(self, requests):
                self.requests = requests
                self.replies = []

            def __iter__(self):
                return iter(json.dumps(item) for item in self.requests)

            def send(self, message):
                self.replies.append(json.loads(message))

        connection = Connection([
            {"msg": "method", "id": "a", "method": "enclosure.query"},
            {"msg": "method", "id": "b", "method": "auth.login_with_api_key", "params": ["synthetic-compose-source-key"]},
            {"msg": "method", "id": "c", "method": "enclosure.set_slot_status"},
            {"msg": "method", "id": "d", "method": "enclosure.query"},
        ])
        self.source.handle(connection)
        self.assertIn("error", connection.replies[0])
        self.assertIs(connection.replies[1]["result"], True)
        self.assertIn("error", connection.replies[2])
        self.assertEqual(connection.replies[3]["result"][0]["elements"], [{"slot": 1, "status": "Not installed"}])

    def test_runtime_source_launch_is_detached_then_readiness_checked_as_inherited_user(self):
        from unittest.mock import call

        prefix = ("docker", "compose", "--project-name", "synthetic")
        source = "/app/config/compose-runtime-source.py"
        with patch.object(self.matrix, "_run") as run:
            self.matrix._start_mapping_source(prefix)
        self.assertEqual(run.call_args_list, [
            call((*prefix, "exec", "-d", "-T", "enclosure-ui", "python", source)),
            call((*prefix, "exec", "-T", "enclosure-ui", "python", source, "--check")),
        ])

    def test_source_start_failure_uses_existing_variant_teardown(self):
        from unittest.mock import Mock

        matrix = self.matrix
        with contextlib.ExitStack() as stack:
            for name in ("_prepare_variant_root", "_assert_compose_resources_removed", "_verify_runtime_contract", "_safe_diagnostics"):
                stack.enter_context(patch.object(matrix, name))
            stack.enter_context(patch.object(matrix, "_verify_rendered_contract", return_value={"services": {"enclosure-ui": {"user": "0:0"}}}))
            stack.enter_context(patch.object(matrix, "_run", return_value=Mock(stdout="enclosure-ui\n")))
            stack.enter_context(patch.object(matrix, "_start_mapping_source", side_effect=RuntimeError("source unavailable")))
            cleanup = stack.enter_context(patch.object(matrix, "_cleanup_variant"))
            mapping = stack.enter_context(patch.object(matrix, "_verify_mapping_cycle"))
            with self.assertRaisesRegex(RuntimeError, "source unavailable"):
                matrix._run_variant(Path("synthetic"), matrix.VARIANTS[0], compose_path=ROOT / "docker-compose.yml",
                                    config_fixture=FIXTURE, image="sha256:" + "a" * 64,
                                    ports=matrix.Ports(19080, 19081, 19082), source_commit="b" * 40)
            cleanup.assert_called_once()
            mapping.assert_not_called()

    def test_standalone_producer_can_be_started_checked_stopped_and_restarted(self):
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        source = ROOT / "tests/fixtures/compose-runtime-source.py"
        environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
        for _ in range(2):
            with subprocess.Popen([sys.executable, str(source), "--port", str(port)],
                                  env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                try:
                    result = subprocess.run([sys.executable, str(source), "--port", str(port), "--check"],
                                            env=environment, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                finally:
                    process.terminate()
                    process.communicate(timeout=5)
            with socket.socket() as listener:
                self.assertNotEqual(listener.connect_ex(("127.0.0.1", port)), 0)


class ComposeRuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.matrix = load_file("matrix", SCRIPT)

    def failure(self, body, url="http://user:secret@example.invalid/api/slots/0/mapping?token=secret"):
        with patch.object(self.matrix, "_request", return_value=(409, body)):
            with self.assertRaises(RuntimeError) as raised:
                self.matrix._require_status(url, 200, method="POST", payload={"password": "secret"})
        message = str(raised.exception)
        self.assertNotIn("secret", message)
        self.assertNotIn("example.invalid", message)
        self.assertLessEqual(len(message), 512)
        return message

    def test_failure_retains_route_and_allowlisted_error_and_detail(self):
        detail = "The disk in this bay could not be checked right now. Nothing was saved. Reload and try again."
        message = self.failure(json.dumps({"error": "mapping_disk_unverifiable", "detail": detail}).encode())
        self.assertIn("POST /api/slots/0/mapping", message)
        self.assertIn("mapping_disk_unverifiable", message)
        self.assertIn(detail, message)
        self.assertIn("expected 200, got 409", message)

    def test_invalid_json_and_non_object_bodies_are_not_echoed(self):
        for body in (b"secret<not-json>", b'[]', b'"secret"', b'\xff'):
            with self.subTest(body=body):
                self.assertIn("body omitted", self.failure(body))

    def test_secret_fields_unknown_categories_and_detail_are_redacted(self):
        for payload in (
            {"error": "mapping_disk_unverifiable", "detail": "token=secret", "password": "secret"},
            {"error": "secret", "detail": "secret"},
            {"detail": {"token": "secret"}},
        ):
            with self.subTest(payload=payload):
                message = self.failure(json.dumps(payload).encode())
                self.assertNotIn("password", message)
                self.assertNotIn("token", message)

    def test_unbounded_body_and_unrecognized_route_are_omitted(self):
        self.assertIn("body omitted", self.failure(b'{"error":"mapping_disk_unverifiable","detail":"' + b"x" * 20000 + b'"}'))
        self.assertIn("route omitted", self.failure(b'{}', "http://example.invalid/api/secret/" + "x" * 2000))

    def test_http_error_body_is_retained_only_up_to_the_diagnostic_limit(self):
        import io
        import urllib.error
        from email.message import Message

        error = urllib.error.HTTPError("http://example.invalid/healthz", 409, "conflict", Message(), io.BytesIO(b"x" * 20000))
        with patch.object(self.matrix.urllib.request, "urlopen", side_effect=error):
            status, body = self.matrix._request("http://example.invalid/healthz")
        self.assertEqual(status, 409)
        self.assertEqual(body, b"x" * 4097)

    def test_expected_status_returns_the_original_bytes(self):
        body = b'{"ok":true,"detail":"unchanged"}'
        with patch.object(self.matrix, "_request", return_value=(200, body)):
            self.assertIs(self.matrix._require_status("http://example.invalid/healthz", 200), body)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        sys.path.insert(0, str(ROOT))
        print(json.dumps(probe(sys.argv[2], sys.argv[3])))
    else:
        unittest.main()
