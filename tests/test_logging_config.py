from __future__ import annotations

import logging
import os
import unittest
from unittest.mock import patch

from app.config import PathConfig, Settings
from app.logging_config import JsonFormatter, configure_logging, configure_service_logging


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
