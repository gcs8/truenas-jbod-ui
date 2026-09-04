from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client.parser import text_string_to_metric_families

# Must precede admin_service.main, which builds its app at import time.
from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN
from app.config import Settings, SystemConfig, TrueNASConfig
from app.logging_config import JsonFormatter, SafeTextFormatter
from app.metrics import (
    ScheduledBackupStatusCollector,
    install_metrics,
    observe_backup_operation,
    observe_history_collection_run,
    observe_history_retention_run,
    set_history_collection_schedule_overrun,
    set_history_collector_running,
)
from app.request_context import (
    current_parent_request_id,
    current_request_id,
    request_id_headers,
    validate_request_id,
)
from app.models.domain import InventorySnapshot, SlotView, SmartSummaryView
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.snapshot_export import (
    EXPORT_HISTORY_CACHE,
    EXPORT_RENDER_CACHE,
    EXPORT_ZIP_CACHE,
    SnapshotExportService,
)
from app.services.truenas_ws import TrueNASRawData


async def invoke_asgi(
    app: FastAPI,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "GET",
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers or [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    return messages


def response_body(messages: list[dict[str, object]]) -> str:
    body_chunks = [
        message.get("body", b"")
        for message in messages
        if message.get("type") == "http.response.body"
    ]
    return b"".join(body_chunks).decode("utf-8")


def response_headers(messages: list[dict[str, object]]) -> dict[str, str]:
    start = next(message for message in messages if message.get("type") == "http.response.start")
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }


def build_inventory_service(
    *,
    temp_dir: str,
    system_id: str = "metrics-system",
    platform: str = "core",
    truenas_client=None,
) -> InventoryService:
    settings = Settings()
    system = SystemConfig(id=system_id, truenas=TrueNASConfig(platform=platform))
    return InventoryService(
        settings,
        system,
        truenas_client or AsyncMock(),
        AsyncMock(),
        None,
        MappingStore(f"{temp_dir}\\slot_mappings.json"),
        ProfileRegistry(settings),
        SlotDetailStore(f"{temp_dir}\\slot_detail_cache.json"),
    )


class MetricsRouteTests(unittest.TestCase):
    def test_operations_docs_define_request_and_backup_metric_privacy_contract(self) -> None:
        operations_doc = (
            Path(__file__).resolve().parents[1]
            / "wiki"
            / "Operations-Logging-and-Metrics.md"
        ).read_text(encoding="utf-8")

        for required in (
            "`X-Request-ID`",
            "`truenas_jbod_ui_backup_operations_total`",
            "`truenas_jbod_ui_backup_operation_duration_seconds`",
            "`operation` values: `inspect`, `import`, or `unknown`",
            "`outcome` values: `success`, `rejected`, or `error`",
            "Request IDs never become metric labels",
            "raw Uvicorn access log is disabled",
            "Performance warnings reuse the same request ID and normalized route",
            "exception messages",
        ):
            self.assertIn(required, operations_doc)

    def test_observability_middleware_generates_and_propagates_server_request_id(self) -> None:
        app = FastAPI()
        incoming_request_id = "a" * 32

        with patch.dict("os.environ", {"METRICS_ENABLED": "false"}, clear=False):
            install_metrics(app, service_name="test-request-id", version="0.0.0-test")

        @app.get("/items/{item_id}")
        async def item(item_id: str) -> JSONResponse:
            return JSONResponse(
                {
                    "item": item_id,
                    "request_id": current_request_id(),
                    "parent_request_id": current_parent_request_id(),
                    "outbound_headers": request_id_headers(
                        {
                            "Accept": "application/json",
                            "x-request-id": "client-controlled",
                        }
                    ),
                }
            )

        messages = asyncio.run(
            invoke_asgi(
                app,
                "/items/private-system-name",
                headers=[(b"x-request-id", incoming_request_id.encode("ascii"))],
            )
        )
        payload = json.loads(response_body(messages))
        headers = response_headers(messages)

        self.assertRegex(payload["request_id"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(payload["request_id"], incoming_request_id)
        self.assertEqual(payload["parent_request_id"], incoming_request_id)
        self.assertEqual(payload["outbound_headers"]["X-Request-ID"], payload["request_id"])
        self.assertEqual(payload["outbound_headers"]["Accept"], "application/json")
        self.assertNotIn("x-request-id", payload["outbound_headers"])
        self.assertEqual(headers["x-request-id"], payload["request_id"])
        self.assertIsNone(current_request_id())
        self.assertIsNone(current_parent_request_id())

    def test_parent_request_id_validation_requires_exact_shape(self) -> None:
        valid = "a" * 32
        self.assertEqual(validate_request_id(valid), valid)
        for invalid in (
            f" {valid}",
            f"{valid} ",
            valid.upper(),
            "a" * 31,
            "a" * 33,
            "../private/history.db",
        ):
            with self.subTest(value=invalid):
                self.assertIsNone(validate_request_id(invalid))

    def test_all_three_services_return_server_request_ids(self) -> None:
        from admin_service.config import AdminSettings
        from admin_service.main import app as admin_app, create_app as create_admin_app
        from app.main import app as ui_app
        from history_service.main import app as history_app

        for service_app in (ui_app, history_app, admin_app):
            with self.subTest(title=service_app.title):
                messages = asyncio.run(
                    invoke_asgi(
                        service_app,
                        "/livez",
                        headers=[(b"x-request-id", b"forged-client-value")],
                    )
                )
                request_id = response_headers(messages)["x-request-id"]
                self.assertRegex(request_id, r"^[0-9a-f]{32}$")
                self.assertNotEqual(request_id, "forged-client-value")

        with patch(
            "admin_service.main.get_admin_settings",
            return_value=AdminSettings(
                auth_mode="basic",
                auth_username="synthetic-user",
                auth_password="synthetic-password",
                public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            ),
        ):
            basic_admin_app = create_admin_app()
        unauthorized = asyncio.run(
            invoke_asgi(
                basic_admin_app,
                "/",
                headers=[(b"x-request-id", b"a" * 32)],
            )
        )
        self.assertEqual(
            next(message["status"] for message in unauthorized if message.get("type") == "http.response.start"),
            401,
        )
        self.assertRegex(response_headers(unauthorized)["x-request-id"], r"^[0-9a-f]{32}$")

    def test_request_error_log_uses_route_template_and_omits_exception_text(self) -> None:
        app = FastAPI()
        with patch.dict("os.environ", {"METRICS_ENABLED": "false"}, clear=False):
            install_metrics(app, service_name="test-error-log", version="0.0.0-test")

        @app.get("/items/{item_id}")
        async def fail(item_id: str) -> JSONResponse:
            raise RuntimeError(f"secret-token /private/{item_id}.db")

        with self.assertLogs("app.observability", level="ERROR") as captured:
            messages = asyncio.run(invoke_asgi(app, "/items/system-alpha"))

        payload = json.loads(JsonFormatter(service_name="test-error-log").format(captured.records[-1]))
        error_payload = json.loads(response_body(messages))
        request_id = response_headers(messages)["x-request-id"]
        self.assertEqual(
            next(message["status"] for message in messages if message.get("type") == "http.response.start"),
            500,
        )
        self.assertEqual(
            error_payload,
            {
                "ok": False,
                "detail": "Unhandled application error; see application logs.",
                "request_id": request_id,
            },
        )
        self.assertEqual(payload["route"], "/items/{item_id}")
        self.assertEqual(payload["exception_class"], "RuntimeError")
        self.assertEqual(payload["request_id"], request_id)
        serialized = json.dumps(payload)
        for forbidden in ("system-alpha", "secret-token", "/private/"):
            self.assertNotIn(forbidden, serialized)

    def test_unhandled_route_error_logs_one_traceback_record_carrying_the_request_id(self) -> None:
        app = FastAPI()
        with patch.dict("os.environ", {"METRICS_ENABLED": "false"}, clear=False):
            install_metrics(app, service_name="test-traceback-log", version="0.0.0-test")

        @app.get("/boom")
        async def boom() -> JSONResponse:
            raise RuntimeError("synthetic middleware failure")

        with self.assertLogs("app.observability", level="ERROR") as captured:
            messages = asyncio.run(invoke_asgi(app, "/boom"))

        request_id = response_headers(messages)["x-request-id"]
        traceback_records = [record for record in captured.records if record.exc_info]
        self.assertEqual(len(traceback_records), 1)
        self.assertEqual(getattr(traceback_records[0], "request_id", None), request_id)
        self.assertIsNone(captured.records[-1].exc_info)

        json_line = JsonFormatter(service_name="test-traceback-log").format(traceback_records[0])
        text_line = SafeTextFormatter(service_name="test-traceback-log").format(traceback_records[0])
        for rendered in (json_line, text_line):
            self.assertIn("Traceback", rendered)
            self.assertIn("boom", rendered)

        self.assertEqual(
            next(message["status"] for message in messages if message.get("type") == "http.response.start"),
            500,
        )
        self.assertEqual(
            json.loads(response_body(messages)),
            {
                "ok": False,
                "detail": "Unhandled application error; see application logs.",
                "request_id": request_id,
            },
        )

    def test_http_method_label_is_restricted_to_known_methods(self) -> None:
        app = FastAPI()
        service_name = "test-method-label"
        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=service_name, version="0.0.0-test")

        @app.get("/method-probe")
        async def method_probe() -> JSONResponse:
            return JSONResponse({"ok": True})

        with self.assertLogs("app.observability", level="INFO") as captured:
            asyncio.run(invoke_asgi(app, "/method-probe", method="PROPFIND"))
            asyncio.run(invoke_asgi(app, "/method-probe", method="GET"))

        metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertNotIn("PROPFIND", metrics_text)
        self.assertIn(f'method="other",route="/method-probe",service="{service_name}"', metrics_text)
        self.assertIn(f'method="GET",route="/method-probe",service="{service_name}"', metrics_text)
        self.assertEqual(
            [
                getattr(record, "method", None)
                for record in captured.records
                if getattr(record, "component", None) == service_name
            ],
            ["other", "GET"],
        )

    def test_json_observability_record_excludes_exception_text_and_unknown_fields(self) -> None:
        formatter = JsonFormatter(service_name="enclosure-admin")
        try:
            raise RuntimeError("secret-token /private/history.db system-alpha")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="app.observability",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="http_request_error",
            args=(),
            exc_info=exc_info,
        )
        record.event = "http_request_error"
        record.request_id = "b" * 32
        record.method = "POST"
        record.route = "/api/admin/backup/import"
        record.status_code = 500
        record.duration_ms = 12.5
        record.private_path = "/private/history.db"
        record.system_id = "system-alpha"
        record.password = "secret-token"

        payload = json.loads(formatter.format(record))

        self.assertEqual(payload["request_id"], "b" * 32)
        self.assertEqual(payload["exception_class"], "RuntimeError")
        self.assertEqual(payload["route"], "/api/admin/backup/import")
        serialized = json.dumps(payload)
        for forbidden in ("secret-token", "/private/history.db", "system-alpha", "Traceback"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("exc_info", payload)

    def test_install_metrics_mounts_metrics_and_records_http_samples(self) -> None:
        app = FastAPI()
        service_name = "test-metrics-route"

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=service_name, version="0.0.0-test")

        @app.get("/ping")
        async def ping() -> JSONResponse:
            return JSONResponse({"ok": True})

        asyncio.run(invoke_asgi(app, "/ping"))
        metrics_messages = asyncio.run(invoke_asgi(app, "/metrics"))
        metrics_text = response_body(metrics_messages)

        self.assertIn('truenas_jbod_ui_http_requests_total', metrics_text)
        self.assertIn(f'service="{service_name}"', metrics_text)
        self.assertIn('route="/ping"', metrics_text)
        self.assertIn('truenas_jbod_ui_build_info', metrics_text)

    def test_install_metrics_can_be_disabled(self) -> None:
        app = FastAPI()

        with patch.dict("os.environ", {"METRICS_ENABLED": "false"}, clear=False):
            install_metrics(app, service_name="test-metrics-disabled", version="0.0.0-test")

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertNotIn("/metrics", paths)

        with self.assertLogs("app.observability", level="INFO") as captured:
            messages = asyncio.run(invoke_asgi(app, "/metrics"))

        self.assertEqual(
            next(message["status"] for message in messages if message.get("type") == "http.response.start"),
            404,
        )
        self.assertRegex(response_headers(messages)["x-request-id"], r"^[0-9a-f]{32}$")
        self.assertEqual(getattr(captured.records[-1], "route"), "unmatched")

    def test_backup_operation_metrics_use_only_bounded_labels(self) -> None:
        app = FastAPI()
        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name="test-backup-metrics", version="0.0.0-test")
            observe_backup_operation(
                service_name="enclosure-admin",
                operation="inspect",
                outcome="success",
                duration_seconds=1.25,
            )
            observe_backup_operation(
                service_name="enclosure-admin",
                operation="private/system-alpha/history.db",
                outcome="secret-token",
                duration_seconds=2.5,
            )

        metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertIn("truenas_jbod_ui_backup_operations_total", metrics_text)
        self.assertIn('operation="inspect",outcome="success"', metrics_text)
        self.assertIn('operation="unknown",outcome="error"', metrics_text)
        self.assertIn("truenas_jbod_ui_backup_operation_duration_seconds", metrics_text)
        for forbidden in ("private/system-alpha/history.db", "secret-token", "request_id", "exception"):
            self.assertNotIn(forbidden, metrics_text)


class SnapshotExportMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        EXPORT_HISTORY_CACHE.clear()
        EXPORT_RENDER_CACHE.clear()
        EXPORT_ZIP_CACHE.clear()

    def test_snapshot_export_metrics_publish_bounded_cache_outcomes_and_sizes(self) -> None:
        app = FastAPI()
        service_name = "test-snapshot-export-cache"
        settings = Settings()
        object.__setattr__(settings.app, "export_cache_max_bytes", 5)

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name="test-scrape", version="0.0.0-test")
            exporter = SnapshotExportService(
                settings,
                AsyncMock(),
                AsyncMock(),
                metrics_service_name=service_name,
            )
            exporter._store_cached_value(EXPORT_HISTORY_CACHE, "history-key", b"123")
            self.assertEqual(exporter._get_cached_value(EXPORT_HISTORY_CACHE, "history-key"), b"123")
            self.assertIsNone(exporter._get_cached_value(EXPORT_HISTORY_CACHE, "missing"))
            exporter._store_cached_value(EXPORT_RENDER_CACHE, "render-key", b"1234")
            exporter._store_cached_value(EXPORT_ZIP_CACHE, "zip-key", b"123456")

        metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertIn("truenas_jbod_ui_snapshot_export_cache_requests_total", metrics_text)
        self.assertIn(f'service="{service_name}"', metrics_text)
        self.assertIn('cache="history"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)
        self.assertIn('cache_state="miss"', metrics_text)
        self.assertIn("truenas_jbod_ui_snapshot_export_cache_evictions_total", metrics_text)
        self.assertIn('reason="bytes"', metrics_text)
        self.assertIn("truenas_jbod_ui_snapshot_export_cache_rejections_total", metrics_text)
        self.assertIn('cache="zip"', metrics_text)
        self.assertIn('reason="oversized"', metrics_text)
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_snapshot_export_cache_entries\{{cache="render",service="{service_name}"\}} 1\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_snapshot_export_cache_bytes\{{cache="render",service="{service_name}"\}} 4\.0',
        )
        serialized_labels = json.dumps(
            [
                sample.labels
                for family in text_string_to_metric_families(metrics_text)
                for sample in family.samples
            ],
            sort_keys=True,
        )
        for forbidden in ("history-key", "render-key", "zip-key", "123456"):
            self.assertNotIn(forbidden, serialized_labels)


class HistoryMetricsTests(unittest.TestCase):
    def test_history_metrics_publish_collector_state(self) -> None:
        app = FastAPI()
        scrape_service_name = "test-metrics-scrape"
        history_service_name = "test-history-metrics"

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=scrape_service_name, version="0.0.0-test")
            set_history_collector_running(history_service_name, True)
            set_history_collection_schedule_overrun(history_service_name, 2.5)
            observe_history_collection_run(
                service_name=history_service_name,
                result="success",
                duration_seconds=1.25,
                status={
                    "last_scope_count": 3,
                    "last_error": None,
                    "poll_interval_seconds": 300,
                    "background_consecutive_failures": 3,
                    "background_backoff_delay_seconds": 900,
                    "failure_backoff_max_seconds": 900,
                    "last_smart_failure_evidence_disks": 2,
                    "last_max_temperature_celsius": 61,
                    "last_smart_evidence_at": "2026-04-27T16:02:30+00:00",
                    "last_inventory_at": "2026-04-27T16:00:00+00:00",
                    "last_fast_metrics_at": "2026-04-27T16:01:00+00:00",
                    "last_slow_metrics_at": "2026-04-27T16:02:00+00:00",
                    "last_success_at": "2026-04-27T16:03:00+00:00",
                    "last_backup_at": "2026-04-27T16:04:00+00:00",
                },
                counts={
                    "tracked_slots": 24,
                    "event_count": 48,
                    "metric_sample_count": 96,
                },
            )

        metrics_messages = asyncio.run(invoke_asgi(app, "/metrics"))
        metrics_text = response_body(metrics_messages)

        self.assertIn('truenas_jbod_ui_history_collection_runs_total', metrics_text)
        self.assertIn(f'service="{history_service_name}"', metrics_text)
        self.assertIn('result="success"', metrics_text)
        self.assertIn('truenas_jbod_ui_history_last_scope_count', metrics_text)
        self.assertIn('truenas_jbod_ui_history_tracked_slots', metrics_text)
        self.assertIn('truenas_jbod_ui_history_collection_schedule_overrun_seconds', metrics_text)
        self.assertIn(
            f'truenas_jbod_ui_history_collection_schedule_overrun_seconds{{service="{history_service_name}"}} 2.5',
            metrics_text,
        )
        expected_samples = (
            "truenas_jbod_ui_history_collection_interval_seconds",
            "truenas_jbod_ui_history_collection_consecutive_failures",
            "truenas_jbod_ui_history_collection_failure_backoff_seconds",
            "truenas_jbod_ui_history_collection_failure_backoff_max_seconds",
            "truenas_jbod_ui_history_smart_failure_evidence_disks",
            "truenas_jbod_ui_history_max_temperature_celsius",
            "truenas_jbod_ui_history_smart_evidence_timestamp_seconds",
        )
        for metric_name in expected_samples:
            self.assertIn(metric_name, metrics_text)
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_collection_interval_seconds\{{service="{history_service_name}"\}} 300\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_collection_failure_backoff_seconds\{{service="{history_service_name}"\}} 900\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_smart_failure_evidence_disks\{{service="{history_service_name}"\}} 2\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_max_temperature_celsius\{{service="{history_service_name}"\}} 61\.0',
        )
        for forbidden_label in ("system_id=", "enclosure_id=", "slot=", "serial=", "device_name="):
            matching_lines = [line for line in metrics_text.splitlines() if any(name in line for name in expected_samples)]
            self.assertNotIn(forbidden_label, "\n".join(matching_lines))

    def test_history_metrics_publish_retention_results(self) -> None:
        app = FastAPI()
        scrape_service_name = "test-retention-metrics-scrape"
        history_service_name = "test-history-retention"

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=scrape_service_name, version="0.0.0-test")
            observe_history_retention_run(
                service_name=history_service_name,
                result="success",
                duration_seconds=0.75,
                removed_rows={
                    "metric_samples": 12,
                    "events": 3,
                    "hourly_rollups": 2,
                    "daily_rollups": 1,
                },
                completed_at="2026-07-01T00:00:00+00:00",
            )

        metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertIn("truenas_jbod_ui_history_retention_runs_total", metrics_text)
        self.assertIn('result="success"', metrics_text)
        self.assertIn("truenas_jbod_ui_history_retention_duration_seconds", metrics_text)
        self.assertIn("truenas_jbod_ui_history_retention_rows_removed_total", metrics_text)
        self.assertIn('table="metric_samples"', metrics_text)
        self.assertIn("truenas_jbod_ui_history_last_retention_timestamp_seconds", metrics_text)


class ScheduledBackupMetricsTests(unittest.TestCase):
    def test_metrics_module_imports_with_valid_scheduled_backup_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduled-backup.json"
            status_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "included_groups": ["history_db"],
                        "success_count": 1,
                        "failure_count": 0,
                        "last_attempt_at": "2030-01-01T03:04:05+00:00",
                        "last_success_at": "2030-01-01T03:04:05+00:00",
                        "last_failure_at": None,
                        "last_size_bytes": 12345,
                        "last_sha256": "a" * 64,
                        "last_artifact_name": (
                            "jbod-scheduled-backup-20300101T030405Z-1234abcd.tar.zst.enc"
                        ),
                        "last_absent_groups": [],
                        "last_retention_removed": 0,
                        "last_error_code": None,
                    }
                ),
                encoding="utf-8",
            )
            status_file.chmod(0o600)
            result = subprocess.run(
                [sys.executable, "-c", "import app.metrics"],
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SCHEDULED_BACKUP_STATUS_FILE": str(status_file),
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scheduled_backup_metrics_reject_malformed_durable_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduled-backup.json"
            status_file.write_text(
                json.dumps({"schema_version": 1, "success_count": "not-an-integer"}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"SCHEDULED_BACKUP_STATUS_FILE": str(status_file)},
                clear=False,
            ):
                self.assertIsNone(ScheduledBackupStatusCollector._read_status())

    def test_scheduled_backup_metrics_read_durable_status_without_private_labels(self) -> None:
        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduled-backup.json"
            status_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "success_count": 4,
                        "failure_count": 2,
                        "last_attempt_at": "2030-01-01T03:04:05+00:00",
                        "last_success_at": (
                            datetime.now(timezone.utc) - timedelta(seconds=90)
                        ).isoformat(),
                        "last_failure_at": "2030-01-01T03:04:05+00:00",
                        "last_size_bytes": 12345,
                        "last_sha256": "a" * 64,
                        "last_error_code": None,
                        "last_artifact_name": (
                            "jbod-scheduled-backup-20300101T030405Z-1234abcd.tar.zst.enc"
                        ),
                        "included_groups": ["private-group"],
                        "last_absent_groups": [],
                        "last_retention_removed": 1,
                    }
                ),
                encoding="utf-8",
            )
            status_file.chmod(0o600)
            with patch.dict(
                "os.environ",
                {
                    "METRICS_ENABLED": "true",
                    "METRICS_PATH": "/metrics",
                    "SCHEDULED_BACKUP_STATUS_FILE": str(status_file),
                },
                clear=False,
            ):
                install_metrics(app, service_name="test-scheduled-scrape", version="0.0.0-test")
                metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertIn("truenas_jbod_ui_scheduled_backup_runs_total", metrics_text)
        self.assertIn(
            'truenas_jbod_ui_scheduled_backup_runs_total{result="success",service="enclosure-backup"} 4.0',
            metrics_text,
        )
        self.assertIn(
            'truenas_jbod_ui_scheduled_backup_runs_total{result="error",service="enclosure-backup"} 2.0',
            metrics_text,
        )
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_success_timestamp_seconds", metrics_text)
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_failure_timestamp_seconds", metrics_text)
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_success_age_seconds", metrics_text)
        self.assertIn(
            'truenas_jbod_ui_scheduled_backup_last_size_bytes{service="enclosure-backup"} 12345.0',
            metrics_text,
        )
        self.assertNotIn("private-name", metrics_text)
        self.assertNotIn("private-group", metrics_text)
        self.assertNotIn(str(status_file), metrics_text)

    def test_scheduled_backup_metrics_do_not_report_cleared_incompatible_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduled-backup.json"
            status_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "success_count": 4,
                        "failure_count": 3,
                        "last_attempt_at": "2030-01-02T03:04:05+00:00",
                        "last_success_at": None,
                        "last_failure_at": "2030-01-02T03:04:05+00:00",
                        "last_size_bytes": None,
                        "last_sha256": None,
                        "last_error_code": "RuntimeError",
                        "last_artifact_name": None,
                        "included_groups": ["config_file", "history_db"],
                        "last_absent_groups": [],
                        "last_retention_removed": 0,
                    }
                ),
                encoding="utf-8",
            )
            status_file.chmod(0o600)
            with patch.dict(
                "os.environ",
                {"SCHEDULED_BACKUP_STATUS_FILE": str(status_file)},
                clear=False,
            ):
                metrics = list(ScheduledBackupStatusCollector().collect())

        metric_names = {metric.name for metric in metrics}
        self.assertIn("truenas_jbod_ui_scheduled_backup_runs", metric_names)
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_failure_timestamp_seconds", metric_names)
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_error", metric_names)
        self.assertNotIn(
            "truenas_jbod_ui_scheduled_backup_last_success_timestamp_seconds",
            metric_names,
        )
        self.assertNotIn(
            "truenas_jbod_ui_scheduled_backup_last_success_age_seconds",
            metric_names,
        )
        self.assertNotIn("truenas_jbod_ui_scheduled_backup_last_size_bytes", metric_names)


class InventoryMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_metrics_publish_snapshot_cache_states(self) -> None:
        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-inventory", version="0.0.0-test")
                service = build_inventory_service(temp_dir=temp_dir, system_id="metrics-snapshot-system")
                service._build_snapshot = AsyncMock(
                    return_value=InventorySnapshot(
                        slots=[SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="da0")],
                        refresh_interval_seconds=30,
                        selected_system_id="metrics-snapshot-system",
                        selected_system_platform="core",
                    )
                )

                await service.get_snapshot(force_refresh=True)
                await service.get_snapshot()

        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)

        self.assertIn("truenas_jbod_ui_inventory_snapshot_requests_total", metrics_text)
        self.assertIn('system_id="metrics-snapshot-system"', metrics_text)
        self.assertIn('cache_state="forced-refresh"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)
        self.assertIn("truenas_jbod_ui_inventory_snapshot_cache_entries", metrics_text)

    async def test_inventory_metrics_publish_source_bundle_states(self) -> None:
        class DummyTrueNASClient:
            async def fetch_all(self) -> TrueNASRawData:
                return TrueNASRawData(
                    enclosures=[],
                    disks=[],
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-bundle", version="0.0.0-test")
                service = build_inventory_service(
                    temp_dir=temp_dir,
                    system_id="metrics-bundle-system",
                    truenas_client=DummyTrueNASClient(),
                )

                await service._get_inventory_source_bundle(force_refresh=True)
                await service._get_inventory_source_bundle()

        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)

        self.assertIn("truenas_jbod_ui_inventory_source_bundle_requests_total", metrics_text)
        self.assertIn('system_id="metrics-bundle-system"', metrics_text)
        self.assertIn('cache_state="forced-refresh"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)
        self.assertIn("truenas_jbod_ui_inventory_source_bundle_build_duration_seconds", metrics_text)

    async def test_inventory_metrics_publish_smart_cache_states(self) -> None:
        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-smart", version="0.0.0-test")
                service = build_inventory_service(temp_dir=temp_dir, system_id="metrics-smart-system")
                slot_view = SlotView(
                    slot=7,
                    slot_label="07",
                    row_index=0,
                    column_index=7,
                    device_name="da7",
                )
                cache_key = service._smart_cache_key(slot_view)
                service._smart_cache[cache_key] = SmartSummaryView(available=True, power_on_hours=1200)
                service._smart_cache_until[cache_key] = datetime.now(timezone.utc) + timedelta(minutes=5)

                summary = await service._get_slot_smart_summary_for_slot_view(slot_view)

        self.assertTrue(summary.available)
        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)

        self.assertIn("truenas_jbod_ui_smart_summary_requests_total", metrics_text)
        self.assertIn('system_id="metrics-smart-system"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)

    async def test_smart_cache_retention_eviction_updates_entry_gauge_before_cache_hit_returns(self) -> None:
        app = FastAPI()
        system_id = "metrics-smart-eviction"
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-smart-eviction", version="0.0.0-test")
                service = build_inventory_service(temp_dir=temp_dir, system_id=system_id)
                expired_slot = SlotView(
                    slot=6,
                    slot_label="06",
                    row_index=0,
                    column_index=6,
                    device_name="da6",
                )
                fresh_slot = SlotView(
                    slot=7,
                    slot_label="07",
                    row_index=0,
                    column_index=7,
                    device_name="da7",
                )
                expired_key = service._smart_cache_key(expired_slot)
                fresh_key = service._smart_cache_key(fresh_slot)
                service._smart_cache[expired_key] = SmartSummaryView(available=True, power_on_hours=600)
                service._smart_cache[fresh_key] = SmartSummaryView(available=True, power_on_hours=700)
                service._smart_cache_until[expired_key] = (
                    datetime.now(timezone.utc)
                    - service._smart_cache_stale_retention()
                    - timedelta(seconds=1)
                )
                service._smart_cache_until[fresh_key] = datetime.now(timezone.utc) + timedelta(minutes=5)
                service._observe_inventory_cache_metrics()

                summary = await service._get_slot_smart_summary_for_slot_view(fresh_slot)

        self.assertEqual(summary.power_on_hours, 700)
        self.assertEqual(len(service._smart_cache), 1)
        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)
        gauge_line = next(
            line
            for line in metrics_text.splitlines()
            if line.startswith("truenas_jbod_ui_smart_summary_cache_entries{")
            and f'system_id="{system_id}"' in line
        )
        self.assertTrue(gauge_line.endswith(" 1.0"), gauge_line)
