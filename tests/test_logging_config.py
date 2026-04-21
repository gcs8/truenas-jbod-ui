from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from app.config import PathConfig, Settings
from app.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root_logger = logging.getLogger()
        self.original_handlers = list(self.root_logger.handlers)
        self.original_level = self.root_logger.level
        for handler in list(self.root_logger.handlers):
            self.root_logger.removeHandler(handler)
            handler.close()
        if hasattr(configure_logging, "_configured"):
            delattr(configure_logging, "_configured")

    def tearDown(self) -> None:
        for handler in list(self.root_logger.handlers):
            self.root_logger.removeHandler(handler)
            handler.close()
        self.root_logger.setLevel(self.original_level)
        for handler in self.original_handlers:
            self.root_logger.addHandler(handler)
        if hasattr(configure_logging, "_configured"):
            delattr(configure_logging, "_configured")

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
