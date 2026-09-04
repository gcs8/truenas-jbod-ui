from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest.mock import patch

from app.config import PathConfig, Settings
from app.logging_config import (
    INCLUDE_TRACEBACK_FIELD,
    JsonFormatter,
    SafeTextFormatter,
    configure_logging,
    configure_service_logging,
)


class LoggingConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root_logger = logging.getLogger()
        self.original_handlers = list(self.root_logger.handlers)
        self.original_level = self.root_logger.level
        self.original_log_format = os.environ.get("LOG_FORMAT")
        for handler in list(self.root_logger.handlers):
            self.root_logger.removeHandler(handler)
            handler.close()
        for target in (configure_logging, configure_service_logging):
            if hasattr(target, "_configured"):
                delattr(target, "_configured")

    def tearDown(self) -> None:
        for handler in list(self.root_logger.handlers):
            self.root_logger.removeHandler(handler)
            handler.close()
        self.root_logger.setLevel(self.original_level)
        for handler in self.original_handlers:
            self.root_logger.addHandler(handler)
        for target in (configure_logging, configure_service_logging):
            if hasattr(target, "_configured"):
                delattr(target, "_configured")
        if self.original_log_format is None:
            os.environ.pop("LOG_FORMAT", None)
        else:
            os.environ["LOG_FORMAT"] = self.original_log_format

    def test_configure_logging_falls_back_to_stream_handler_when_file_open_fails(self) -> None:
        settings = Settings(
            paths=PathConfig(
                mapping_file="/tmp/slot_mappings.json",
                log_file="/tmp/logs/app.log",
                profile_file="/tmp/profiles.yaml",
                slot_detail_cache_file="/tmp/slot_detail_cache.json",
            )
        )

        with (
            patch("app.logging_config.RotatingFileHandler", side_effect=PermissionError("denied")),
            self.assertLogs("app.logging_config", level="WARNING") as captured,
        ):
            configure_logging(settings)

        self.assertTrue(
            any(
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)
                for handler in self.root_logger.handlers
            )
        )
        self.assertFalse(
            any(isinstance(handler, logging.FileHandler) for handler in self.root_logger.handlers)
        )
        self.assertIn("File logging disabled", captured.output[0])

    def test_json_formatter_includes_service_name(self) -> None:
        formatter = JsonFormatter(service_name="enclosure-ui")
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello world",
            args=(),
            exc_info=None,
        )

        payload = formatter.format(record)

        self.assertIn('"service": "enclosure-ui"', payload)
        self.assertIn('"message": "hello world"', payload)

    def test_safe_text_formatter_keeps_bounded_request_fields_without_exception_text(self) -> None:
        formatter = SafeTextFormatter(service_name="enclosure-admin")
        try:
            raise RuntimeError("secret-token /private/history.db system-alpha")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="app.observability",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="http_request_complete",
            args=(),
            exc_info=exc_info,
        )
        record.event = "http_request_complete"
        record.request_id = "a" * 32
        record.method = "POST"
        record.route = "/api/admin/backup/import"
        record.status_code = 500
        record.duration_ms = 12.5
        record.release = "0.22.3"
        record.private_path = "/private/history.db"
        record.system_id = "system-alpha"
        record.password = "secret-token"

        rendered = formatter.format(record)

        for expected in (
            "service=enclosure-admin",
            f"request_id={'a' * 32}",
            "method=POST",
            "route=/api/admin/backup/import",
            "status_code=500",
            "duration_ms=12.5",
            "release=0.22.3",
            "exception_class=RuntimeError",
        ):
            self.assertIn(expected, rendered)
        for forbidden in ("secret-token", "/private/history.db", "system-alpha", "Traceback"):
            self.assertNotIn(forbidden, rendered)

    def test_opt_in_tracebacks_keep_frames_without_exception_values(self) -> None:
        def raise_private_error(message: str) -> None:
            raise RuntimeError(message)

        private_message = "secret-token /private/history.db system-alpha"
        exc_info = None
        try:
            raise_private_error(private_message)
        except RuntimeError:
            exc_info = sys.exc_info()
        self.assertIsNotNone(exc_info)
        record = logging.LogRecord(
            name="app.observability",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="http_request_error",
            args=(),
            exc_info=exc_info,
        )
        setattr(record, INCLUDE_TRACEBACK_FIELD, True)

        rendered_lines = (
            JsonFormatter(service_name="enclosure-ui").format(record),
            SafeTextFormatter(service_name="enclosure-ui").format(record),
        )

        for rendered in rendered_lines:
            self.assertIn("Traceback", rendered)
            self.assertIn("raise_private_error", rendered)
            self.assertIn("RuntimeError", rendered)
            for forbidden in ("secret-token", "/private/history.db", "system-alpha"):
                self.assertNotIn(forbidden, rendered)

    def test_configure_logging_uses_json_stream_when_requested(self) -> None:
        settings = Settings(
            paths=PathConfig(
                mapping_file="/tmp/slot_mappings.json",
                log_file="/tmp/logs/app.log",
                profile_file="/tmp/profiles.yaml",
                slot_detail_cache_file="/tmp/slot_detail_cache.json",
            )
        )

        with patch.dict(os.environ, {"LOG_FORMAT": "json"}, clear=False):
            configure_logging(settings)

        stream_handler = next(
            handler
            for handler in self.root_logger.handlers
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        )
        self.assertIsInstance(stream_handler.formatter, JsonFormatter)

    def test_configure_service_logging_disables_raw_uvicorn_access_log(self) -> None:
        access_logger = logging.getLogger("uvicorn.access")
        error_logger = logging.getLogger("uvicorn.error")
        original_access_disabled = access_logger.disabled
        original_error_disabled = error_logger.disabled
        self.addCleanup(setattr, access_logger, "disabled", original_access_disabled)
        self.addCleanup(setattr, error_logger, "disabled", original_error_disabled)

        configure_service_logging(
            log_level="INFO",
            service_name="enclosure-ui",
        )

        self.assertTrue(access_logger.disabled)
        self.assertFalse(error_logger.disabled)
        self.assertTrue(error_logger.propagate)
