"""Synthetic readiness recovery, timing boundaries and bounded diagnostics."""
from __future__ import annotations

import io
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from tests.test_perf_harness import run_perf_harness as harness
from tests import test_background_refresh_deadlines as deadlines
from tests.test_background_refresh_busy_routes import SHELF, SYSTEM_ID, call_app
from app import route_support, routes
from app.services import inventory as inventory_module


ESTIMATE = "/api/export/enclosure-snapshot/estimate"
BUSY_DETAIL = "The inventory is still refreshing. Try again in a moment."


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.headers = {}


def busy(*, status=503, retry_after="2", detail=BUSY_DETAIL):
    stream = io.BytesIO(json.dumps({"detail": detail}).encode())
    error = HTTPError("https://host.example.test/private?selector=SECRET", status,
                      "private response reason", {"Retry-After": retry_after}, stream)
    return error, stream


class PerfHarnessReadinessTests(unittest.TestCase):
    def test_safe_estimate_survives_more_than_three_busy_replies(self):
        clock = Clock()
        errors = [busy() for _ in range(4)]
        calls = 0

        def open_url(request, timeout):
            nonlocal calls
            calls += 1
            self.assertLessEqual(timeout, 180 - clock.now)
            clock.now += 5
            if calls <= len(errors):
                raise errors[calls - 1][0]
            return Response({"selected_allowed": True})

        with (patch.object(harness, "urlopen", side_effect=open_url),
              patch.object(harness.time, "monotonic", clock.monotonic),
              patch.object(harness.time, "sleep", clock.sleep)):
            response = harness.ApiClient("https://host.example.test").post_json_with_headers(ESTIMATE, {})
        self.assertTrue(response.data["selected_allowed"])
        self.assertEqual(calls, 5)
        self.assertEqual(clock.sleeps, [2, 2, 2, 2])
        self.assertTrue(all(stream.closed for _, stream in errors))
        metrics = harness.build_request_metrics(response)
        self.assertEqual(metrics.request_count, 5)
        self.assertEqual(metrics.metadata_counts["busy_wait_ms"], 28000)
        self.assertEqual(metrics.metadata_counts["busy_replies"], 4)

    def test_never_ready_has_a_deadline_and_private_bounded_diagnostics(self):
        clock = Clock()
        errors = []

        def open_url(request, timeout):
            self.assertLess(len(errors), 27, "readiness polling exceeded the bounded fake-call fixture")
            self.assertLessEqual(timeout, 180 - clock.now)
            clock.now += min(5, timeout)
            error, stream = busy()
            errors.append(stream)
            raise error

        with (patch.object(harness, "urlopen", side_effect=open_url),
              patch.object(harness.time, "monotonic", clock.monotonic),
              patch.object(harness.time, "sleep", clock.sleep)):
            with self.assertRaises(Exception) as caught:
                harness.ApiClient("https://host.example.test", system_id="SECRET").post_json(ESTIMATE, {})
        diagnostic = getattr(caught.exception, "diagnostic", {})
        self.assertEqual(diagnostic.get("reason"), "readiness_deadline_exceeded")
        self.assertEqual(diagnostic["endpoint"], ESTIMATE)
        self.assertEqual(diagnostic["status"], 503)
        self.assertEqual(diagnostic["attempts"], 26)
        self.assertEqual(diagnostic["retry_after_seconds"], 2)
        self.assertEqual(diagnostic["elapsed_seconds"], 180)
        self.assertEqual(diagnostic["body_category"], "inventory_refreshing")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 600)
        self.assertTrue(all(stream.closed for stream in errors))
        self.assertEqual(clock.now, 180)

    def test_writes_forced_reads_and_unlisted_endpoints_are_not_retried(self):
        operations = [
            lambda client: client.get_json("/api/inventory", {"force": "true"}),
            lambda client: client.post_json("/api/mappings/import", {}),
            lambda client: client.post_json("/api/mappings/import/preview", {}),
            lambda client: client.post_json("/api/slots/smart-batch", {"force_refresh": True}),
            lambda client: client.get_json("/api/unlisted"),
        ]
        for operation in operations:
            error, stream = busy()
            with (self.subTest(operation=operation),
                  patch.object(harness, "urlopen", side_effect=error) as opened,
                  patch.object(harness.time, "sleep") as sleep,
                  self.assertRaises(HTTPError)):
                operation(harness.ApiClient("https://host.example.test"))
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()
            self.assertTrue(stream.closed)

    def test_nonretryable_errors_close_without_retaining_body_or_headers(self):
        for status, retry_after in [(500, "2"), (503, "SECRET"), (503, "9" * 1000), (503, "")]:
            error, stream = busy(status=status, retry_after=retry_after, detail="SECRET" * 10000)
            with (self.subTest(status=status, retry_after=retry_after[:10]),
                  patch.object(harness, "urlopen", side_effect=error) as opened,
                  patch.object(harness.time, "sleep") as sleep,
                  self.assertRaises(HTTPError)):
                harness.ApiClient("https://host.example.test").post_json(ESTIMATE, {})
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()
            self.assertTrue(stream.closed)

    def test_zero_retry_after_makes_bounded_progress(self):
        clock = Clock()
        streams = []

        def open_url(*args, **kwargs):
            error, stream = busy(retry_after="0", detail="SECRET")
            streams.append(stream)
            raise error

        with (patch.object(harness, "urlopen", side_effect=open_url),
              patch.object(harness.time, "monotonic", clock.monotonic),
              patch.object(harness.time, "sleep", clock.sleep)):
            client = harness.ApiClient("https://host.example.test", readiness_timeout_seconds=1)
            with self.assertRaises(harness.ReadinessDeadlineExceeded) as caught:
                client.get_json("/api/inventory")
        self.assertEqual(caught.exception.diagnostic["body_category"], "other")
        self.assertEqual(clock.now, 1)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertTrue(all(delay > 0 for delay in clock.sleeps))

    def test_retry_after_is_honoured_only_inside_remaining_budget(self):
        clock = Clock()
        error, stream = busy(retry_after="60")
        with (patch.object(harness, "urlopen", side_effect=error) as opened,
              patch.object(harness.time, "monotonic", clock.monotonic),
              patch.object(harness.time, "sleep", clock.sleep)):
            client = harness.ApiClient("https://host.example.test", readiness_timeout_seconds=3)
            with self.assertRaises(harness.ReadinessDeadlineExceeded) as caught:
                client.get_json("/api/inventory")
        self.assertEqual(clock.sleeps, [3])
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(caught.exception.diagnostic["retry_after_seconds"], 60)
        self.assertTrue(stream.closed)

    def test_invalid_cli_budget_is_refused_before_any_request(self):
        for value in ("0", "-1", "nan", "inf"):
            with (self.subTest(value=value), patch.object(sys, "argv", ["perf", "--readiness-timeout-seconds", value]),
                  patch.object(harness, "urlopen") as opened,
                  patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught):
                harness.main()
            self.assertEqual(caught.exception.code, 2)
            opened.assert_not_called()

    def test_error_body_read_is_bounded_and_response_is_closed(self):
        clock = Clock()
        error, stream = busy(detail="SECRET" * 10000, retry_after="60")
        with (patch.object(stream, "read", wraps=stream.read) as read,
              patch.object(harness, "urlopen", side_effect=error),
              patch.object(harness.time, "monotonic", clock.monotonic),
              patch.object(harness.time, "sleep", clock.sleep)):
            client = harness.ApiClient("https://host.example.test", readiness_timeout_seconds=1)
            with self.assertRaises(harness.ReadinessDeadlineExceeded) as caught:
                client.get_json("/api/inventory")
        read.assert_called_once_with(harness.ERROR_BODY_LIMIT)
        self.assertTrue(stream.closed)
        self.assertEqual(caught.exception.diagnostic["body_category"], "other")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_error_body_timeout_variants_at_deadline_keep_diagnostics_and_close(self):
        for exception in (TimeoutError("SECRET"), socket.timeout("SECRET"), URLError(socket.timeout("SECRET"))):
            clock = Clock()
            error, stream = busy()

            def read_body(limit):
                self.assertEqual(limit, harness.ERROR_BODY_LIMIT)
                clock.now += 3
                raise exception

            with (self.subTest(exception=type(exception).__name__),
                  patch.object(stream, "read", side_effect=read_body),
                  patch.object(harness, "urlopen", side_effect=error),
                  patch.object(harness.time, "monotonic", clock.monotonic)):
                with self.assertRaises(harness.ReadinessDeadlineExceeded) as caught:
                    harness.ApiClient("https://host.example.test", readiness_timeout_seconds=3).post_json(ESTIMATE, {})
                self.assertEqual(caught.exception.diagnostic["status"], 503)
                self.assertEqual(caught.exception.diagnostic["retry_after_seconds"], 2)
                self.assertEqual(caught.exception.diagnostic["elapsed_seconds"], 3)
                self.assertEqual(caught.exception.diagnostic["body_category"], "transport_timeout")
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertTrue(stream.closed)

    def test_error_body_failure_before_deadline_or_not_timeout_is_preserved(self):
        for exception, elapsed in ((TimeoutError("SECRET"), 0), (URLError(TimeoutError("SECRET")), 0),
                                   (URLError(ValueError("SECRET")), 3)):
            clock = Clock()
            error, stream = busy()

            def read_body(limit):
                clock.now += elapsed
                raise exception

            with (self.subTest(exception=type(exception).__name__, elapsed=elapsed),
                  patch.object(stream, "read", side_effect=read_body),
                  patch.object(harness, "urlopen", side_effect=error),
                  patch.object(harness.time, "monotonic", clock.monotonic)):
                with self.assertRaises(type(exception)) as caught:
                    harness.ApiClient("https://host.example.test", readiness_timeout_seconds=3).post_json(ESTIMATE, {})
                self.assertIs(caught.exception, exception)
                self.assertTrue(stream.closed)

    def test_main_reports_stalled_local_503_body_and_closes_response(self):
        release = threading.Event()
        headers_sent = threading.Event()
        responses = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                content = b'{"slots":[]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(503)
                self.send_header("Retry-After", "2")
                self.send_header("Content-Length", "81")
                self.end_headers()
                self.wfile.flush()
                headers_sent.set()
                release.wait(5)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        real_open = harness.urlopen

        def record_error(request, timeout):
            try:
                return real_open(request, timeout=timeout)
            except HTTPError as error:
                responses.append(error.fp)
                raise

        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output.json"
                records = Path(directory) / "records"
                argv = ["perf", "--base-url", f"http://127.0.0.1:{server.server_port}",
                        "--iterations", "1", "--skip-mappings-import-roundtrip",
                        "--readiness-timeout-seconds", "0.1", "--output", str(output),
                        "--record-dir", str(records)]
                with (patch.object(sys, "argv", argv),
                      patch.object(harness, "urlopen", side_effect=record_error)):
                    result = harness.main()
                self.assertEqual(result, 1)
                payload = json.loads(output.read_text())
                self.assertEqual(payload, json.loads((records / "failure.json").read_text()))
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["failure"]["reason"], "readiness_deadline_exceeded")
                self.assertEqual(payload["failure"]["endpoint"], ESTIMATE)
                self.assertEqual(payload["failure"]["status"], 503)
                self.assertEqual(payload["failure"]["retry_after_seconds"], 2)
                self.assertEqual(payload["failure"]["attempts"], 1)
                self.assertEqual(payload["failure"]["body_category"], "transport_timeout")
                self.assertTrue(headers_sent.is_set(), "test must reach the withheld error body")
                self.assertFalse(release.is_set(), "closure must precede server cleanup")
                self.assertEqual(len(responses), 1)
                self.assertTrue(responses[0].closed)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_safe_sibling_reads_recover_and_record_attempts(self):
        for path in sorted(harness.SAFE_GET_PATHS):
            error, stream = busy()
            clock = Clock()
            with (self.subTest(path=path), patch.object(harness, "urlopen", side_effect=[error, Response({"ok": True})]),
                  patch.object(harness.time, "monotonic", clock.monotonic),
                  patch.object(harness.time, "sleep", clock.sleep)):
                response = harness.ApiClient("https://host.example.test").get_json_with_headers(path)
            self.assertEqual(response.attempts, 2)
            self.assertEqual(clock.sleeps, [2])
            self.assertTrue(stream.closed)

    def test_environment_budget_is_used_and_cli_overrides_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(harness.os.environ, {harness.READINESS_TIMEOUT_ENV: "17"}):
            result, payload = self.run_main(directory, Clock(), stuck=True)
            self.assertEqual(result, 1)
            self.assertEqual(payload["failure"]["elapsed_seconds"], 17)
            result, payload = self.run_main(directory, Clock(), stuck=True,
                                            extra_args=("--readiness-timeout-seconds", "20"))
            self.assertEqual(result, 1)
            self.assertEqual(payload["failure"]["elapsed_seconds"], 20)

    def test_transport_timeout_is_not_mislabeled_as_deadline_exhaustion(self):
        clock = Clock()
        with (patch.object(harness, "urlopen", side_effect=TimeoutError("synthetic timeout")),
              patch.object(harness.time, "monotonic", clock.monotonic)):
            with self.assertRaises(TimeoutError):
                harness.ApiClient("https://host.example.test").get_json("/api/inventory")

    def test_transport_timeout_at_deadline_has_safe_diagnostics(self):
        clock = Clock()

        def open_url(request, timeout):
            clock.now += timeout
            raise TimeoutError("SECRET")

        with (patch.object(harness, "urlopen", side_effect=open_url),
              patch.object(harness.time, "monotonic", clock.monotonic)):
            with self.assertRaises(harness.ReadinessDeadlineExceeded) as caught:
                harness.ApiClient("https://host.example.test", readiness_timeout_seconds=3).get_json("/api/inventory")
        self.assertEqual(caught.exception.diagnostic["body_category"], "transport_timeout")
        self.assertEqual(caught.exception.diagnostic["elapsed_seconds"], 3)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_wrapped_transport_failure_before_deadline_or_not_timeout_is_preserved(self):
        for reason, elapsed in ((TimeoutError("SECRET"), 0), (ValueError("SECRET"), 180)):
            clock = Clock()
            error = URLError(reason)

            def open_url(request, timeout):
                clock.now += elapsed
                raise error

            with (self.subTest(reason=type(reason).__name__),
                  patch.object(harness, "urlopen", side_effect=open_url),
                  patch.object(harness.time, "monotonic", clock.monotonic)):
                with self.assertRaises(URLError) as caught:
                    harness.ApiClient("https://host.example.test").get_json("/api/inventory")
            self.assertIs(caught.exception, error)

    def test_main_records_wrapped_connection_timeout_at_the_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            result, payload = self.run_main(directory, Clock(), wrapped_timeout=True,
                                            extra_args=("--readiness-timeout-seconds", "3"))
        self.assertEqual(result, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failure"]["endpoint"], ESTIMATE)
        self.assertEqual(payload["failure"]["elapsed_seconds"], 3)
        self.assertEqual(payload["failure"]["attempts"], 1)
        self.assertEqual(payload["failure"]["body_category"], "transport_timeout")
        self.assertNotIn("SECRET", json.dumps(payload))

    def run_main(self, directory, clock, *, stuck=False, wrapped_timeout=False, extra_args=()):
        estimate_calls = 0
        imported = False

        def open_url(request, timeout):
            nonlocal estimate_calls, imported
            path = urlsplit(request.full_url).path
            if path == "/api/mappings/import/preview":
                return Response({"revision": "a" * 64, "import_digest": "b" * 64})
            if path == "/api/mappings/import":
                imported = True
            if path == ESTIMATE:
                self.assertTrue(imported)
                estimate_calls += 1
                if wrapped_timeout:
                    clock.now += timeout
                    raise URLError(TimeoutError("SECRET"))
                if stuck or estimate_calls <= 4:
                    clock.now += min(timeout, 5)
                    raise busy()[0]
                clock.now += 0.25
            if path == "/api/mappings/export":
                return Response({"schema_version": 1, "mappings": []})
            return Response({"ok": True, "slots": []})

        output = Path(directory) / "result.json"
        argv = ["run_perf_harness.py", "--iterations", "1", "--no-record", "--output", str(output), *extra_args]
        with (patch.object(harness, "urlopen", side_effect=open_url),
              patch.object(harness.time, "monotonic", clock.monotonic),
              patch.object(harness.time, "perf_counter", clock.monotonic),
              patch.object(harness.time, "sleep", clock.sleep),
              patch.object(harness, "detect_git_context", return_value=harness.GitContext(None, None, False)),
              patch.object(sys, "argv", argv)):
            result = harness.main()
        return result, json.loads(output.read_text())

    def test_mapping_readiness_is_separate_from_warm_measurement_and_full_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            result, payload = self.run_main(directory, Clock())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "passed")
        ready = payload["readiness"][0]
        self.assertEqual(ready["duration_ms"], 28250)
        self.assertEqual(ready["request_count"], 5)
        rows = {row["name"]: row for row in payload["results"]}
        warm = rows["snapshot_export_estimate"]
        workflow = rows["snapshot_export_estimate_workflow"]
        self.assertEqual(warm["duration_ms"], 250)
        self.assertEqual(warm["request_count"], 1)
        self.assertEqual(workflow["duration_ms"], 28500)
        self.assertEqual(workflow["request_count"], 6)
        self.assertEqual(workflow["metadata_counts"]["busy_wait_ms"], 28000)
        markdown = harness.render_markdown(payload)
        self.assertIn("Readiness waits", markdown)
        self.assertIn("28250", markdown)

    def test_main_writes_failed_output_and_returns_nonzero_on_exhaustion(self):
        with tempfile.TemporaryDirectory() as directory:
            result, payload = self.run_main(directory, Clock(), stuck=True,
                                            extra_args=("--readiness-timeout-seconds", "20"))
        self.assertEqual(result, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failure"]["reason"], "readiness_deadline_exceeded")
        self.assertEqual(payload["failure"]["elapsed_seconds"], 20)
        self.assertNotIn("base_url", payload)


class EmptyImportReadinessWorkflowTests(deadlines.RefreshDeadlineTests):
    async def test_empty_mapping_import_leaves_no_view_until_held_refresh_finishes(self):
        route_support.SNAPSHOT_EXPORT_SOURCE_CACHE.clear()
        self.addCleanup(route_support.SNAPSHOT_EXPORT_SOURCE_CACHE.clear)
        exporter = Mock()
        exporter.estimate_enclosure_snapshot_export = AsyncMock(return_value={"selected_allowed": True})
        with (patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60),
              patch.object(routes, "get_snapshot_export_service", return_value=exporter)):
            cached = self.service._cache[SHELF]
            await self.start_hung_source_refresh()
            query = f"system_id={SYSTEM_ID}&enclosure_id={SHELF}"
            bundle = {"schema_version": 1, "mappings": []}
            status, _, body = await call_app(self.application, "POST", "/api/mappings/import/preview", query=query, body=bundle)
            self.assertEqual(status, 200, body)
            confirmation = harness.build_mapping_import_confirmation(bundle, json.loads(body))
            status, _, body = await call_app(self.application, "POST", "/api/mappings/import", query=query, body=confirmation)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["imported"], 0)
            self.assertIn("refresh has not finished", json.loads(body)["warning"])
            self.assertFalse(self.service._cache)
            status, headers, body = await call_app(self.application, "POST", ESTIMATE, query=query, body={})
            self.assertEqual(status, 503, body)
            self.assertEqual(headers["retry-after"], "2")
            self.assertEqual(json.loads(body)["detail"], BUSY_DETAIL)
            exporter.estimate_enclosure_snapshot_export.assert_not_awaited()
            self.assertEqual(self.appliance.reads, 2)
            self.appliance.answer.set()
            await self.settle()
            status, _, body = await call_app(self.application, "POST", ESTIMATE, query=query, body={})
            self.assertEqual(status, 200, body)
            self.assertTrue(json.loads(body)["selected_allowed"])
            current = exporter.estimate_enclosure_snapshot_export.call_args.kwargs["snapshot"]
            self.assertGreater(current.last_updated, cached.last_updated)
            self.assertEqual(self.appliance.reads, 2)
            self.assert_budget_respected()
