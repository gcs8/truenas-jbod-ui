from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import PerfConfig, Settings, get_settings
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
