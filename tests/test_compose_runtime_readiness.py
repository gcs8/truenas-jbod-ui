"""Native producer lifecycle and history-reader ordering for the Compose matrix."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from tests.test_compose_runtime_occupancy import SCRIPT, load_file  # noqa: E402


def lifecycle_probe(directory, schedule):
    """Keep the real controller, producer, app, collector and persistence paths."""
    from websockets.sync.server import serve

    matrix = load_file("matrix_readiness", SCRIPT)
    producer = load_file("readiness_source", ROOT / "tests/fixtures/compose-runtime-source.py")
    root = Path(directory) / "ui-history"
    ports = matrix.Ports(19080, 19081, 19082)
    variant = next(item for item in matrix.VARIANTS if item.name == "ui-history")
    loop = asyncio.new_event_loop()
    source = thread = application = collector = None
    source_port = None
    history_running = False
    generation = -1
    events, requests, history = [], [], []
    target = {"initial": 0, "alias": 1, "mapping": 2, "cached": 2, "legacy": 2}[schedule]

    def start_source():
        nonlocal source, thread, source_port
        assert source is None
        source = serve(producer.handle, "127.0.0.1", source_port or 0)
        if source_port is None:
            source_port = source.socket.getsockname()[1]
            config = root / "config/config.yaml"
            config.write_text(config.read_text().replace(":19443", f":{source_port}"))
        thread = threading.Thread(target=source.serve_forever)
        thread.start()
        events.append([generation, "producer-start"])

    def stop_source():
        nonlocal source, thread
        if source is not None:
            source.shutdown()
            assert thread is not None
            thread.join(5)
            assert not thread.is_alive()
            source = thread = None
            events.append([generation, "producer-stop"])

    def recreate():
        nonlocal application, collector, generation
        from app import main as app_main, route_support
        from app.config import get_settings
        from app.settings_reload import SettingsRuntime
        from history_service.collector import HistoryCollector
        from history_service.config import HistorySettings
        from history_service.store import HistoryStore

        previous = route_support.get_inventory_registry()
        runtime = SettingsRuntime(get_settings)
        app_main.SETTINGS_RUNTIME = route_support.SETTINGS_RUNTIME = runtime
        application = app_main.create_app()
        assert route_support.get_inventory_registry() is not previous
        generation += 1
        if collector is None:
            settings = get_settings()
            assert all(not item.ssh.enabled for item in settings.systems)
            for name in ("mapping_file", "slot_detail_cache_file", "log_file"):
                assert Path(getattr(settings.paths, name)).resolve().is_relative_to(root)
            collector = HistoryCollector(
                HistorySettings(source_base_url="http://enclosure-ui:8000"),
                HistoryStore(str(root / "history/history.db")),
            )
        events.append([generation, "ui-created"])

    def request(url, *, method="GET", payload=None, authenticated=False, origin=None, actor="matrix"):
        parts = urllib.parse.urlsplit(url)
        headers = [(b"host", b"127.0.0.1:19080"), (b"content-type", b"application/json")]
        if authenticated:
            headers.append((b"authorization", matrix._authorization_header().encode()))
        if origin:
            headers.append((b"origin", origin.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "scheme": "http", "method": method, "path": parts.path or "/",
                 "raw_path": (parts.path or "/").encode(), "query_string": parts.query.encode(),
                 "headers": headers, "client": ("127.0.0.1", 1),
                 "server": ("127.0.0.1", 19080), "root_path": ""}
        sent = []

        async def invoke():
            completed = asyncio.Event()
            pending = [{"type": "http.request", "body": b"" if payload is None else json.dumps(payload).encode(),
                        "more_body": False}]

            async def receive():
                if pending:
                    return pending.pop()
                await completed.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    completed.set()

            await asyncio.wait_for(application(scope, receive, send), 20)

        if loop.is_running():
            asyncio.run_coroutine_threadsafe(invoke(), loop).result(timeout=25)
        else:
            loop.run_until_complete(invoke())
        status = next(item["status"] for item in sent if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        parsed = json.loads(body) if body.startswith((b"{", b"[")) else None
        requests.append({"generation": generation, "actor": actor, "method": method,
                         "path": parts.path, "query": parts.query, "status": status, "body": parsed})
        if parts.path == "/api/inventory":
            events.append([generation, actor + "-inventory"])
        if method == "DELETE" and status != 200:
            saved = json.loads((root / "data/slot_mappings.json").read_bytes())
            assert len(saved["slot_mappings"]) == 1
            assert next(iter(saved["slot_mappings"].values()))["notes"] == "Matrix ui-history"
        return status, body

    def urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        host = urllib.parse.urlsplit(url).hostname
        if host == "enclosure-history":
            return io.BytesIO(b'{"status":"ok","collector":{"running":false},"counts":{}}')
        assert host == "enclosure-ui", "unexpected HTTP transport"
        status, body = request(url, authenticated=True, actor="history")
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "synthetic response", {}, io.BytesIO(body))
        return io.BytesIO(body)

    def collect():
        try:
            loop.run_until_complete(collector.run_once(
                force_fast=True, force_slow=False, include_due_intervals=False,
                cached_root_only=True, collection_kind="background",
            ))
            history.append({"generation": generation, "result": "ok"})
        except Exception as exc:
            history.append({"generation": generation, "result": type(exc).__name__,
                            "kind": getattr(exc, "kind", None), "stage": collector._failure_stage})

    def run(command, **kwargs):
        nonlocal history_running
        command = list(command)
        if command[:3] == ["sudo", "install", "-d"]:
            for name in command[command.index("-g") + 2:]:
                Path(name).mkdir(parents=True, exist_ok=True)
        elif command[:2] == ["sudo", "install"]:
            shutil.copyfile(command[-2], command[-1])
        elif "stop" in command:
            assert command[-1] == "enclosure-history"
            history_running = False
            events.append([generation, "history-stop"])
        elif "restart" in command:
            assert command[-1] == "enclosure-ui"
            stop_source()
            recreate()
            if schedule in {"cached", "legacy"} and generation == target:
                # A controlled cold reader also proves forced-refresh recovery,
                # independently of quiescing the real history consumer.
                collect()
        elif "up" in command:
            services = set(command) & {"enclosure-ui", "enclosure-history"}
            if "enclosure-ui" in services and application is None:
                # Reserve the configured port, but do not keep a producer alive.
                start_source()
                stop_source()
                recreate()
            if "enclosure-history" in services:
                history_running = True
                events.append([generation, "history-start"])
                # The historical controller permits a cold read in this window.
                if source is not None or generation == target:
                    collect()
            elif history_running and generation == target:
                collect()
        elif "exec" in command:
            if "-d" in command:
                start_source()
            else:
                assert command[-1] == "--check"
                producer.check(source_port)
                events.append([generation, "producer-ready"])
        elif "ps" in command:
            return Mock(stdout="enclosure-ui\nenclosure-history\n")
        else:
            raise AssertionError("unexpected Compose transport")
        return Mock(stdout="")

    os.environ.update({
        "APP_CONFIG_PATH": str(root / "config/config.yaml"), "APP_SERVICE_ROLE": "ui",
        "RELEASE_CHECK_ENABLED": "false", "READ_UI_AUTH_MODE": "basic",
        "READ_UI_AUTH_USERNAME": matrix.AUTH_USERNAME, "READ_UI_AUTH_PASSWORD": matrix.AUTH_PASSWORD,
        "ADMIN_AUTH_MODE": "basic", "ADMIN_AUTH_USERNAME": matrix.AUTH_USERNAME,
        "ADMIN_AUTH_PASSWORD": matrix.AUTH_PASSWORD, "APP_PUBLIC_ORIGIN": "http://127.0.0.1:19080",
        "HISTORY_BACKEND_URL": "http://enclosure-history:8001", "BACKUP_TARGETS_JSON": "[]",
        "BACKUP_CONFIG_ENABLED": "false", "BACKUP_FULL_ENABLED": "false",
        "SCHEDULED_BACKUP_ENABLED": "false", "BACKUP_JOURNAL_PATH": str(root / "journal/changes.jsonl"),
    })
    error = None
    real_inventory_gate = matrix._wait_mapping_inventory

    def inventory_gate(ports):
        if schedule == "legacy" and generation == target:
            return
        real_inventory_gate(ports)

    try:
        with (
            patch.object(matrix, "_wait_mapping_inventory", side_effect=inventory_gate),
            patch.object(matrix, "_run", side_effect=run),
            patch.object(matrix, "_request", side_effect=request),
            patch.object(matrix.urllib.request, "urlopen", side_effect=urlopen),
            patch.object(matrix, "_verify_rendered_contract", return_value={"services": {
                "enclosure-ui": {"user": f"{os.getuid()}:{os.getgid()}"}}}),
            patch.object(matrix, "UI_WRITER_UID", os.getuid()),
            patch.object(matrix, "UI_WRITER_GID", os.getgid()),
            patch.object(matrix, "_assert_compose_resources_removed"),
            patch.object(matrix, "_verify_runtime_contract"),
            patch.object(matrix, "_verify_history"),
            patch.object(matrix, "_safe_diagnostics"),
            patch.object(matrix, "_cleanup_variant"),
            patch.object(matrix, "_read_app_owned_json", side_effect=lambda p: json.loads(p.read_bytes())),
            patch.object(matrix, "_read_app_owned_metadata", side_effect=lambda p: (
                os.getuid(), os.getgid(), p.stat().st_mode, p.stat().st_size)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                matrix._run_variant(Path(directory), variant, compose_path=ROOT / "docker-compose.yml",
                                    config_fixture=ROOT / "tests/fixtures/ci-smoke-config.yaml",
                                    image="sha256:" + "a" * 64, ports=ports, source_commit="b" * 40)
            except RuntimeError as exc:
                error = str(exc)
        path = root / "data/slot_mappings.json"
        return {"error": error, "events": events, "requests": requests, "history": history,
                "store": json.loads(path.read_bytes()) if path.exists() else None}
    finally:
        stop_source()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


class ComposeRuntimeReadinessIntegrationTests(unittest.TestCase):
    def run_probe(self, schedule):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"PATH": os.defpath, "HOME": directory, "TMPDIR": directory,
                           "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", directory, schedule],
                                    cwd=ROOT, env=environment, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_cold_history_cache_is_replaced_by_forced_inventory_before_clear(self):
        observed = self.run_probe("cached")
        self.assertIsNone(observed["error"], observed["error"])
        self.assertIn({"generation": 2, "result": "HistorySourceError", "kind": "source_error_reply",
                       "stage": "scope_enumeration"}, observed["history"])
        cold = next(r for r in observed["requests"] if r["generation"] == 2 and r["actor"] == "history")
        self.assertIs(cold["body"]["sources"]["api"]["ok"], False)
        self.assertIsNone(cold["body"]["selected_enclosure_id"])
        self.assertEqual([r["status"] for r in observed["requests"] if r["method"] == "DELETE"], [200])
        self.assertEqual(observed["store"]["slot_mappings"], {})

    def test_legacy_readiness_leaves_real_503_and_saved_mapping_unchanged(self):
        observed = self.run_probe("legacy")
        self.assertIn("expected 200, got 503", observed["error"])
        clear = next(r for r in observed["requests"] if r["method"] == "DELETE")
        self.assertEqual(clear["body"], {"ok": False, "detail": "Unable to resolve selected enclosure layout."})
        self.assertEqual(len(observed["store"]["slot_mappings"]), 1)
        self.assertEqual(next(iter(observed["store"]["slot_mappings"].values()))["notes"], "Matrix ui-history")

    def test_history_readers_wait_for_real_producer_at_start_and_both_restarts(self):
        for schedule in ("mapping", "alias", "initial"):
            with self.subTest(schedule=schedule), tempfile.TemporaryDirectory() as directory:
                environment = {"PATH": os.defpath, "HOME": directory, "TMPDIR": directory,
                               "PYTHONDONTWRITEBYTECODE": "1"}
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", directory, schedule],
                                        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = json.loads(result.stdout)
                self.assertIsNone(observed["error"], json.dumps({
                    key: observed[key] for key in ("error", "events", "history")}, indent=2))
                self.assertEqual(observed["store"]["slot_mappings"], {})
                mutations = [r for r in observed["requests"] if r["path"] == "/api/slots/0/mapping"]
                self.assertEqual([r["status"] for r in mutations], [409, 200, 200])
                self.assertEqual(observed["history"], [{"generation": n, "result": "ok"} for n in range(3)])
                for generation in range(3):
                    events = [name for number, name in observed["events"] if number == generation]
                    self.assertLess(events.index("producer-ready"), events.index("history-inventory"))
                    self.assertLess(events.index("producer-ready"), events.index("matrix-inventory"))
                    forced = [r for r in observed["requests"] if r["generation"] == generation
                              and r["actor"] == "matrix" and r["path"] == "/api/inventory"
                              and urllib.parse.parse_qs(r["query"]).get("force") == ["true"]]
                    self.assertTrue(forced, events)
                    snapshot = forced[-1]["body"]
                    self.assertIs(snapshot["sources"]["api"]["ok"], True)
                    self.assertEqual(snapshot["selected_enclosure_id"], "matrix-shelf")
                    self.assertEqual(snapshot["slots"][0]["state"], "empty")
                    if generation == 2:
                        self.assertEqual(snapshot["slots"][0]["notes"], "Matrix ui-history")


class ComposeRuntimeReadinessContractTests(unittest.TestCase):
    def setUp(self):
        self.matrix = load_file("matrix_readiness_contract", SCRIPT)
        self.ports = self.matrix.Ports(19080, 19081, 19082)

    def test_changed_default_system_refuses_before_any_mapping_write(self):
        inventory = {"selected_system_id": "another-system", "selected_enclosure_id": None,
                     "slots": [{"slot": 0, "mapping_revision": "a" * 64}]}
        with (patch.object(self.matrix, "_require_status", return_value=json.dumps(inventory).encode()) as request,
              patch.object(self.matrix, "_verify_unread_mapping", side_effect=RuntimeError("paired unread unavailable")) as unread,
              self.assertRaisesRegex(RuntimeError, "paired unread")):
            self.matrix._verify_mapping_cycle(Path("synthetic"), self.matrix.VARIANTS[0], self.ports, ())
        unread.assert_called_once_with("http://127.0.0.1:19080")
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in request.call_args_list))

    def test_missing_unread_system_cannot_be_substituted_by_live_default(self):
        base = "http://example.test"
        inventory = {"selected_system_id": "matrix-live", "slots": [
            {"slot": 0, "mapping_revision": "a" * 64, "presence_known": False}]}
        with (patch.object(self.matrix, "_require_status", return_value=json.dumps(inventory).encode()) as request,
              self.assertRaisesRegex(RuntimeError, "Unread mapping source")):
            self.matrix._verify_unread_mapping(base)
        self.assertEqual(request.call_count, 1)

    def ready_inventory(self):
        return {"selected_system_id": "matrix-live", "selected_enclosure_id": "matrix-shelf",
                "sources": {"api": {"ok": True}}, "slots": [{"slot": 0, "state": "empty",
                "present": False, "identity_state": "known"}]}

    def test_inventory_gate_forces_the_exact_scope_and_recovers_failed_cache(self):
        ready = self.ready_inventory()
        with (patch.object(self.matrix, "_require_status", side_effect=[b'{"sources":{}}', json.dumps(ready).encode()]) as request,
              patch.object(self.matrix.time, "monotonic", side_effect=[0, 0, 0.25]),
              patch.object(self.matrix.time, "sleep") as sleep):
            self.matrix._wait_mapping_inventory(self.ports)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args, (
            "http://127.0.0.1:19080/api/inventory?system_id=matrix-live&enclosure_id=matrix-shelf&force=true", 200))
        self.assertEqual(request.call_args.kwargs, {"authenticated": True})
        sleep.assert_called_once_with(0.25)

    def test_inventory_gate_bounds_unavailable_malformed_and_wrong_scope_evidence(self):
        ready = self.ready_inventory()
        invalid = [b'[]', b'not-json', b'{}',
                   json.dumps({**ready, "selected_system_id": "matrix-unread"}).encode(),
                   json.dumps({**ready, "selected_enclosure_id": None}).encode(),
                   json.dumps({**ready, "sources": {"api": {"ok": False}}}).encode(),
                   json.dumps({**ready, "slots": []}).encode()]
        for field, value in (("present", True), ("state", "unread"), ("identity_state", "unknown"), ("presence_known", False)):
            invalid.append(json.dumps({**ready, "slots": [{**ready["slots"][0], field: value}]}).encode())
        for body in invalid:
            with (self.subTest(body=body), patch.object(self.matrix, "_require_status", return_value=body) as request,
                  patch.object(self.matrix, "MAPPING_WAIT_SECONDS", 1),
                  patch.object(self.matrix.time, "monotonic", side_effect=[0, 0, 1]),
                  patch.object(self.matrix.time, "sleep"), self.assertRaisesRegex(RuntimeError, "selected enclosure layout")):
                self.matrix._wait_mapping_inventory(self.ports)
            request.assert_called_once()
        with (patch.object(self.matrix, "_require_status", side_effect=OSError("secret")),
              patch.object(self.matrix, "MAPPING_WAIT_SECONDS", 1),
              patch.object(self.matrix.time, "monotonic", side_effect=[0, 0, 1]),
              patch.object(self.matrix.time, "sleep"), self.assertRaises(RuntimeError) as raised):
            self.matrix._wait_mapping_inventory(self.ports)
        self.assertNotIn("secret", str(raised.exception))

    def test_inventory_timeout_keeps_last_safe_http_diagnostic(self):
        detail = "The inventory is still refreshing. Try again in a moment."
        body = json.dumps({"detail": detail, "password": "secret"}).encode()
        with (patch.object(self.matrix, "_request", side_effect=[(503, body), OSError("secret")]),
              patch.object(self.matrix.time, "monotonic", side_effect=[0, 0, 0.25, 30]),
              patch.object(self.matrix.time, "sleep"), self.assertRaises(RuntimeError) as raised):
            self.matrix._wait_mapping_inventory(self.ports)
        message = str(raised.exception)
        self.assertIn("got 503", message)
        self.assertIn("GET /api/inventory", message)
        self.assertIn(detail, message)
        self.assertNotIn("secret", message)
        self.assertLess(len(message), 512)

    def test_inventory_timeout_discards_unrecognized_http_body(self):
        with (patch.object(self.matrix, "_request", return_value=(401, b'{"detail":"secret"}')),
              patch.object(self.matrix.time, "monotonic", side_effect=[0, 0, 30]),
              patch.object(self.matrix.time, "sleep"), self.assertRaises(RuntimeError) as raised):
            self.matrix._wait_mapping_inventory(self.ports)
        self.assertIn("got 401", str(raised.exception))
        self.assertIn("body omitted", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_every_ui_variant_quiesces_history_until_restart_readiness(self):
        for variant in self.matrix.VARIANTS:
            if not variant.ui_enabled:
                continue
            with self.subTest(variant=variant.name):
                events = []
                prefix = ("docker", "compose", "--project-name", "synthetic")

                def run(command, **kwargs):
                    if "stop" in command:
                        self.assertEqual(command[-4:], ("stop", "--timeout", "15", "enclosure-history"))
                        events.append("stop-history")
                    elif "restart" in command:
                        events.append("restart-ui")
                    else:
                        self.assertIn("--no-deps", command)
                        self.assertIn("--wait-timeout", command)
                        events.append("start-history" if command[-1] == "enclosure-history" else "wait-ui")

                with (patch.object(self.matrix, "_run", side_effect=run),
                      patch.object(self.matrix, "_start_mapping_source", side_effect=lambda *_: events.append("source-ready")),
                      patch.object(self.matrix, "_wait_mapping_inventory", side_effect=lambda *_: events.append("inventory-ready")),
                      patch.object(self.matrix, "_verify_ui", side_effect=lambda *_: events.append("health"))):
                    self.matrix._restart_ui(prefix, self.ports, history_enabled=variant.history_enabled)
                expected = ["restart-ui", "source-ready", "wait-ui", "inventory-ready", "health"]
                if variant.history_enabled:
                    expected = ["stop-history", *expected, "start-history"]
                self.assertEqual(events, expected)

    def test_failed_restart_readiness_does_not_resume_history(self):
        for helper in ("_start_mapping_source", "_wait_mapping_inventory"):
            with (self.subTest(helper=helper), patch.object(self.matrix, "_run") as run,
                  patch.object(self.matrix, "_start_mapping_source"),
                  patch.object(self.matrix, "_wait_mapping_inventory"),
                  patch.object(self.matrix, helper, side_effect=RuntimeError("synthetic readiness refusal")),
                  patch.object(self.matrix, "_verify_ui") as health,
                  self.assertRaisesRegex(RuntimeError, "readiness refusal")):
                self.matrix._restart_ui((), self.ports, history_enabled=True)
            self.assertFalse(any(call.args[0][-1] == "enclosure-history" and "up" in call.args[0]
                                 for call in run.call_args_list))
            health.assert_not_called()

    def test_all_allowlisted_mapping_diagnostics_are_bounded_and_redacted(self):
        from app.services import mapping_store
        from app.services.storage_writability import StorageDirectoryCapacityExhausted, StorageDirectoryUnwritable

        # Some categories belong to the route serializer, not the exception.
        categories = (
            (mapping_store.MappingScopeConflict, "mapping_scope_conflict"),
            (mapping_store.MappingReadBusy, "mapping_read_busy"),
            (mapping_store.MappingDurabilityError, "mapping_durability_indeterminate"),
            (mapping_store.MappingStorageUnwritable, "data_directory_unwritable"),
            (StorageDirectoryCapacityExhausted, "data_directory_capacity_exhausted"),
            (StorageDirectoryUnwritable, "storage_directory_unwritable"),
        )
        from app import route_support

        # These two categories are serialized by handlers, not exception attributes.
        handlers = {
            mapping_store.MappingScopeConflict: route_support.mapping_scope_conflict_exception_handler,
            mapping_store.MappingDurabilityError: route_support.mapping_durability_exception_handler,
        }
        for cls, error in categories:
            with self.subTest(exception=cls.__name__):
                if cls in handlers:
                    response = asyncio.run(handlers[cls](Mock(), RuntimeError("synthetic failure")))
                    self.assertEqual(json.loads(bytes(response.body))["error"], error)
                else:
                    self.assertEqual(getattr(cls, "error_code"), error)
                self.assertIn(cls.public_detail, self.matrix.PUBLIC_HTTP_ERRORS[error])
        self.assertEqual(mapping_store.MappingWriterLockUnavailable.error_code, "mapping_writer_lock_unavailable")
        self.assertEqual(set(mapping_store.MappingWriterLockUnavailable.PUBLIC_DETAILS.values()),
                         set(self.matrix.PUBLIC_HTTP_ERRORS["mapping_writer_lock_unavailable"]))
        for error, details in self.matrix.PUBLIC_HTTP_ERRORS.items():
            for detail in details:
                body = json.dumps({"error": error, "detail": detail, "secret": "secret"}).encode()
                message = self.matrix._http_failure_context("http://example.test/api/slots/0/mapping", "DELETE", body)
                self.assertIn(detail, message)
                self.assertNotIn("secret", message)
                self.assertLessEqual(len(message) + 100, 512)

    def test_detail_only_mapping_failures_keep_only_fixed_public_text(self):
        details = ("Unable to resolve selected enclosure layout.",
                   "Requested enclosure is not available for this system.",
                   "The server is busy. Try again in a moment.")
        for detail in details:
            with self.subTest(detail=detail):
                with patch.object(self.matrix, "_request", return_value=(503, json.dumps({
                    "ok": False, "detail": detail, "password": "secret"}).encode())):
                    with self.assertRaises(RuntimeError) as raised:
                        self.matrix._require_status("http://user:secret@example.test/api/slots/0/mapping?token=secret",
                                                    200, method="DELETE")
                message = str(raised.exception)
                self.assertIn(detail, message)
                self.assertNotIn("secret", message)
                self.assertLessEqual(len(message), 512)
        for body in ({"detail": details[0] + " secret"}, {"error": ["secret"], "detail": {"secret": True}}):
            self.assertIn("body omitted", self.matrix._http_failure_context(
                "http://example.test/api/slots/0/mapping", "DELETE", json.dumps(body).encode()))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        sys.path.insert(0, str(ROOT))
        print(json.dumps(lifecycle_probe(sys.argv[2], sys.argv[3])))
    else:
        unittest.main()
