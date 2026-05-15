from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import (
    PerfConfig,
    Settings,
    get_settings,
    runtime_behavior_settings_payload,
    save_runtime_behavior_overrides,
)
from app.perf import (
    PerfStageSample,
    PerfTrace,
    build_server_timing_header,
    install_perf_timing_middleware,
    perf_stage,
)


class PerfConfigTests(unittest.TestCase):
    def test_get_settings_uses_config_relative_defaults_for_custom_app_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config.yaml"
            config_path.write_text("{}", encoding="utf-8")

            with patch.dict("os.environ", {"APP_CONFIG_PATH": config_path.as_posix()}, clear=False):
                get_settings.cache_clear()
                settings = get_settings()
                get_settings.cache_clear()

            self.assertEqual(Path(settings.config_file), config_path)
            self.assertEqual(Path(settings.paths.profile_file), temp_root / "profiles.yaml")
            self.assertEqual(Path(settings.paths.mapping_file), temp_root / "slot_mappings.json")
            self.assertEqual(Path(settings.paths.slot_detail_cache_file), temp_root / "slot_detail_cache.json")
            self.assertEqual(Path(settings.paths.log_file), temp_root / "app.log")
            self.assertEqual(Path(settings.paths.runtime_overrides_file), temp_root / "runtime-overrides.yaml")
            self.assertEqual(Path(settings.ssh.known_hosts_path), temp_root / "known_hosts")

    def test_get_settings_reads_perf_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config.yaml"
            profile_path = temp_root / "profiles.yaml"
            mapping_path = temp_root / "slot_mappings.json"
            log_path = temp_root / "app.log"

            config_path.write_text(
                "\n".join(
                    [
                        "paths:",
                        f"  profile_file: {profile_path.as_posix()}",
                        f"  mapping_file: {mapping_path.as_posix()}",
                        f"  log_file: {log_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "APP_CONFIG_PATH": config_path.as_posix(),
                    "PERF_TIMING_ENABLED": "true",
                    "PERF_LOG_ALL_REQUESTS": "true",
                    "PERF_SLOW_REQUEST_MS": "1500",
                    "PERF_SLOW_STAGE_MS": "300",
                },
                clear=False,
            ):
                get_settings.cache_clear()
                settings = get_settings()
                get_settings.cache_clear()

            self.assertTrue(settings.perf.enabled)
            self.assertTrue(settings.perf.log_all_requests)
            self.assertEqual(settings.perf.slow_request_ms, 1500)
            self.assertEqual(settings.perf.slow_stage_ms, 300)

    def test_runtime_behavior_overrides_update_admin_owned_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config" / "config.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "\n".join(
                    [
                        "app:",
                        "  cache_ttl_seconds: 10",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"APP_CONFIG_PATH": config_path.as_posix()}, clear=True):
                get_settings.cache_clear()
                settings = get_settings()
                payload = save_runtime_behavior_overrides(
                    settings,
                    {
                        "source_bundle_cache_ttl_seconds": "120",
                        "smart_cache_ttl_seconds": 45,
                    },
                )
                updated_settings = get_settings()
                get_settings.cache_clear()

            self.assertEqual(Path(settings.paths.runtime_overrides_file), config_path.parent / "runtime-overrides.yaml")
            self.assertEqual(updated_settings.app.source_bundle_cache_ttl_seconds, 120)
            self.assertEqual(updated_settings.app.smart_cache_ttl_seconds, 45)
            source_field = next(
                field for field in payload["fields"] if field["key"] == "source_bundle_cache_ttl_seconds"
            )
            self.assertEqual(source_field["owner"], "admin")
            self.assertTrue(source_field["writable"])
            self.assertEqual(source_field["source"], "runtime-overrides.yaml")

    def test_runtime_behavior_env_owned_values_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text("{}", encoding="utf-8")

            with patch.dict(
                "os.environ",
                {
                    "APP_CONFIG_PATH": config_path.as_posix(),
                    "APP_SOURCE_BUNDLE_CACHE_TTL_SECONDS": "90",
                },
                clear=True,
            ):
                get_settings.cache_clear()
                settings = get_settings()
                payload = runtime_behavior_settings_payload(settings)
                with self.assertRaisesRegex(ValueError, "owned by .env"):
                    save_runtime_behavior_overrides(settings, {"source_bundle_cache_ttl_seconds": 120})
                get_settings.cache_clear()

            source_field = next(
                field for field in payload["fields"] if field["key"] == "source_bundle_cache_ttl_seconds"
            )
            self.assertEqual(source_field["value"], 90)
            self.assertEqual(source_field["owner"], ".env")
            self.assertFalse(source_field["writable"])
            self.assertEqual(source_field["source"], "APP_SOURCE_BUNDLE_CACHE_TTL_SECONDS")

    def test_legacy_cache_ttl_env_only_owns_split_fields_when_effective(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "app:",
                        "  source_bundle_cache_ttl_seconds: 60",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "APP_CONFIG_PATH": config_path.as_posix(),
                    "APP_CACHE_TTL": "10",
                },
                clear=True,
            ):
                get_settings.cache_clear()
                settings = get_settings()
                payload = runtime_behavior_settings_payload(settings)
                get_settings.cache_clear()

            snapshot_field = next(
                field for field in payload["fields"] if field["key"] == "snapshot_cache_ttl_seconds"
            )
            source_field = next(
                field for field in payload["fields"] if field["key"] == "source_bundle_cache_ttl_seconds"
            )
            self.assertEqual(settings.app.snapshot_cache_ttl_seconds, 10)
            self.assertEqual(snapshot_field["owner"], ".env")
            self.assertEqual(snapshot_field["source"], "APP_CACHE_TTL")
            self.assertEqual(settings.app.source_bundle_cache_ttl_seconds, 60)
            self.assertEqual(source_field["owner"], "admin")
            self.assertEqual(source_field["source"], "config.yaml")

    def test_runtime_overrides_file_only_applies_behavior_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            config_path = temp_root / "config" / "config.yaml"
            override_path = temp_root / "config" / "runtime-overrides.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("{}", encoding="utf-8")
            override_path.write_text(
                "\n".join(
                    [
                        "app:",
                        "  port: 9099",
                        "  source_bundle_cache_ttl_seconds: 150",
                        "paths:",
                        "  mapping_file: C:/should/not/win.json",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"APP_CONFIG_PATH": config_path.as_posix()}, clear=True):
                get_settings.cache_clear()
                settings = get_settings()
                get_settings.cache_clear()

            self.assertEqual(settings.app.port, 8080)
            self.assertEqual(settings.app.source_bundle_cache_ttl_seconds, 150)
            self.assertNotEqual(Path(settings.paths.mapping_file), Path("C:/should/not/win.json"))


class PerfTraceTests(unittest.TestCase):
    def test_stage_summary_groups_repeated_labels(self) -> None:
        trace = PerfTrace(request_id="abc12345", operation="GET /api/inventory")
        trace.stages = [
            PerfStageSample(label="inventory.build_snapshot", duration_ms=42.5),
            PerfStageSample(label="inventory.api.fetch_all", duration_ms=30.0),
            PerfStageSample(label="inventory.build_snapshot", duration_ms=18.0),
        ]

        summary = trace.stage_summary()

        self.assertIn("inventory.build_snapshot=60.5ms x2", summary)
        self.assertIn("inventory.api.fetch_all=30.0ms", summary)

    def test_build_server_timing_header_lists_total_and_top_stages(self) -> None:
        trace = PerfTrace(request_id="abc12345", operation="GET /api/inventory")
        trace.stages = [
            PerfStageSample(label="inventory.build_snapshot", duration_ms=42.5),
            PerfStageSample(label="inventory.api.fetch_all", duration_ms=30.0),
            PerfStageSample(label="inventory.build_snapshot", duration_ms=18.0),
        ]

        header = build_server_timing_header(trace)

        self.assertIn('app;desc="total";dur=', header)
        self.assertIn('stage-1;desc="inventory.build_snapshot x2";dur=60.5', header)
        self.assertIn('stage-2;desc="inventory.api.fetch_all";dur=30.0', header)

    def test_perf_middleware_sets_request_id_and_logs_stage_summary(self) -> None:
        app = FastAPI()
        settings = Settings(
            perf=PerfConfig(
                enabled=True,
                log_all_requests=True,
                slow_request_ms=60_000,
                slow_stage_ms=60_000,
            )
        )
        install_perf_timing_middleware(app, settings)

        @app.get("/ping")
        async def ping() -> JSONResponse:
            with perf_stage("test.stage"):
                pass
            return JSONResponse({"ok": True})

        async def invoke() -> list[dict[str, object]]:
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
                    "method": "GET",
                    "scheme": "http",
                    "path": "/ping",
                    "raw_path": b"/ping",
                    "query_string": b"",
                    "root_path": "",
                    "headers": [],
                    "client": ("127.0.0.1", 1234),
                    "server": ("testserver", 80),
                },
                receive,
                send,
            )
            return messages

        with self.assertLogs("app.perf", level="INFO") as captured:
            messages = asyncio.run(invoke())

        start = next(message for message in messages if message["type"] == "http.response.start")
        headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in start["headers"]}
        self.assertEqual(start["status"], 200)
        self.assertTrue(headers.get("x-request-id"))
        self.assertIn("server-timing", headers)
        self.assertIn('test.stage', headers["server-timing"])
        self.assertTrue(any("test.stage" in entry for entry in captured.output))
