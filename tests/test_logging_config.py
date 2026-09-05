from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from typing import Any
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
    @staticmethod
    def _render_opt_in_traceback(
        exc_info: tuple[Any, Any, Any],
    ) -> tuple[str, str]:
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
        try:
            json_traceback = json.loads(JsonFormatter(service_name="enclosure-ui").format(record))["traceback"]
            text_line = SafeTextFormatter(service_name="enclosure-ui").format(record)
        except RecursionError as exc:
            raise AssertionError("traceback renderer recursively crashed") from exc
        return json_traceback, text_line

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

    def test_opt_in_tracebacks_keep_chained_frames_without_exception_values(self) -> None:
        def raise_inner_error(message: str) -> None:
            raise ValueError(message)

        def raise_outer_error(inner_message: str, outer_message: str) -> None:
            try:
                raise_inner_error(inner_message)
            except ValueError as exc:
                raise RuntimeError(outer_message) from exc

        inner_message = "inner-secret /private/inner.db"
        outer_message = "outer-secret system-alpha"
        exc_info = None
        try:
            raise_outer_error(inner_message, outer_message)
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
            self.assertIn("raise_inner_error", rendered)
            self.assertIn("ValueError", rendered)
            self.assertIn("raise_outer_error", rendered)
            self.assertIn("RuntimeError", rendered)
            for forbidden in (inner_message, outer_message, "inner-secret", "outer-secret"):
                self.assertNotIn(forbidden, rendered)

    def test_opt_in_tracebacks_traverse_base_exception_group_members_in_stable_order(self) -> None:
        def raise_first_member(message: str) -> None:
            raise ValueError(message)

        def raise_second_member(message: str) -> None:
            raise KeyboardInterrupt(message)

        first_member_secret = "first-member-secret"
        second_member_secret = "second-member-secret"
        outer_group_secret = "outer-group-secret"
        member_exceptions: list[BaseException] = []
        for raiser, message in (
            (raise_first_member, first_member_secret),
            (raise_second_member, second_member_secret),
        ):
            try:
                raiser(message)
            except BaseException as exc:
                member_exceptions.append(exc)

        try:
            raise BaseExceptionGroup(outer_group_secret, member_exceptions)
        except BaseExceptionGroup:
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
        setattr(record, INCLUDE_TRACEBACK_FIELD, True)

        json_traceback = json.loads(
            JsonFormatter(service_name="enclosure-ui").format(record)
        )["traceback"]
        text_line = SafeTextFormatter(service_name="enclosure-ui").format(record)

        self.assertIn(json_traceback, text_line)
        expected_in_order = (
            "BaseExceptionGroup",
            "raise_first_member",
            "ValueError",
            "raise_second_member",
            "KeyboardInterrupt",
        )
        for expected in expected_in_order:
            self.assertIn(expected, json_traceback)
        positions = [json_traceback.index(expected) for expected in expected_in_order]
        self.assertEqual(positions, sorted(positions))
        for forbidden in (
            outer_group_secret,
            first_member_secret,
            second_member_secret,
        ):
            self.assertNotIn(forbidden, json_traceback)
            self.assertNotIn(forbidden, text_line)

    def test_opt_in_tracebacks_traverse_nested_groups_and_chained_causes_cycle_safely(self) -> None:
        def raise_root_cause(message: str) -> None:
            raise KeyError(message)

        def raise_chained_member(root_message: str, member_message: str) -> None:
            try:
                raise_root_cause(root_message)
            except KeyError as exc:
                raise RuntimeError(member_message) from exc

        root_cause_secret = "root-cause-secret"
        chained_member_secret = "chained-member-secret"
        inner_group_secret = "inner-group-secret"
        outer_group_secret = "outer-group-secret"
        chained_member: RuntimeError | None = None
        try:
            raise_chained_member(root_cause_secret, chained_member_secret)
        except RuntimeError as exc:
            chained_member = exc
        self.assertIsNotNone(chained_member)
        if chained_member is None:
            self.fail("chained member was not captured")

        inner_group = ExceptionGroup(inner_group_secret, [chained_member])
        outer_group = ExceptionGroup(outer_group_secret, [inner_group])
        root_cause = chained_member.__cause__
        self.assertIsNotNone(root_cause)
        if root_cause is None:
            self.fail("root cause was not captured")
        root_cause.__context__ = outer_group

        try:
            raise outer_group
        except ExceptionGroup:
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
        setattr(record, INCLUDE_TRACEBACK_FIELD, True)

        json_traceback = json.loads(
            JsonFormatter(service_name="enclosure-ui").format(record)
        )["traceback"]
        text_line = SafeTextFormatter(service_name="enclosure-ui").format(record)

        self.assertIn(json_traceback, text_line)
        for expected in (
            "ExceptionGroup",
            "raise_root_cause",
            "KeyError",
            "raise_chained_member",
            "RuntimeError",
        ):
            self.assertIn(expected, json_traceback)
        self.assertLess(json_traceback.index("KeyError"), json_traceback.index("RuntimeError"))
        for forbidden in (
            root_cause_secret,
            chained_member_secret,
            inner_group_secret,
            outer_group_secret,
        ):
            self.assertNotIn(forbidden, json_traceback)
            self.assertNotIn(forbidden, text_line)

    def test_opt_in_tracebacks_bound_deep_cause_chains_without_recursion(self) -> None:
        root: BaseException = RuntimeError("synthetic-0")
        current = root
        for index in range(1, 1_200):
            cause = RuntimeError(f"synthetic-{index}")
            current.__cause__ = cause
            current = cause
        exc_info = (RuntimeError, root, None)

        json_traceback, text_line = self._render_opt_in_traceback(exc_info)

        self.assertIn("[traceback truncated: exception depth limit]", json_traceback)
        self.assertLessEqual(len(json_traceback), 16_384)
        self.assertIn(json_traceback, text_line)

    def test_opt_in_tracebacks_bound_deep_context_chains_without_recursion(self) -> None:
        root: BaseException = RuntimeError("synthetic-0")
        current = root
        for index in range(1, 1_200):
            context = RuntimeError(f"synthetic-{index}")
            current.__context__ = context
            current = context
        exc_info = (RuntimeError, root, None)

        json_traceback, text_line = self._render_opt_in_traceback(exc_info)

        self.assertIn("[traceback truncated: exception depth limit]", json_traceback)
        self.assertLessEqual(len(json_traceback), 16_384)
        self.assertIn(json_traceback, text_line)

    def test_opt_in_tracebacks_bound_wide_exception_groups_by_node_count(self) -> None:
        group = ExceptionGroup(
            "synthetic-wide-group",
            [ValueError(f"synthetic-member-{index}") for index in range(200)],
        )
        exc_info = (ExceptionGroup, group, None)

        json_traceback, text_line = self._render_opt_in_traceback(exc_info)

        self.assertIn("[traceback truncated: exception node limit]", json_traceback)
        self.assertLessEqual(json_traceback.count("ValueError"), 63)
        self.assertLessEqual(len(json_traceback), 16_384)
        self.assertIn(json_traceback, text_line)

    def test_opt_in_tracebacks_bound_deep_exception_groups_without_recursion(self) -> None:
        nested: Exception = ValueError("synthetic-leaf")
        for index in range(1_200):
            nested = ExceptionGroup(f"synthetic-group-{index}", [nested])
        exc_info = (ExceptionGroup, nested, None)

        json_traceback, text_line = self._render_opt_in_traceback(exc_info)

        self.assertIn("[traceback truncated: exception depth limit]", json_traceback)
        self.assertLessEqual(len(json_traceback), 16_384)
        self.assertIn(json_traceback, text_line)

    def test_opt_in_tracebacks_bound_and_escape_frame_and_exception_metadata(self) -> None:
        exception_type = type("SyntheticError\r\x1b" + "E" * 300, (Exception,), {})
        exception = exception_type("synthetic-message")

        def raise_with_metadata(depth: int) -> None:
            if depth:
                raise_with_metadata(depth - 1)
            raise exception

        raise_with_metadata.__code__ = raise_with_metadata.__code__.replace(
            co_filename="F" * 300 + "TRACE_FILE\r\x1b.py",
            co_name="TRACE_FUNCTION\n\r\x00\x1b" + "N" * 300,
        )
        try:
            raise_with_metadata(100)
        except exception_type:
            exc_info = sys.exc_info()

        json_traceback, text_line = self._render_opt_in_traceback(exc_info)

        self.assertIn("[traceback truncated: frame limit]", json_traceback)
        self.assertLessEqual(json_traceback.count("  File "), 32)
        self.assertNotIn("TRACE_FUNCTION\n", json_traceback)
        for control in ("\r", "\x00", "\x1b"):
            self.assertNotIn(control, json_traceback)
            self.assertNotIn(control, text_line)
        self.assertNotIn("E" * 300, json_traceback)
        self.assertTrue(all(len(line) <= 340 for line in json_traceback.splitlines()))

    def test_opt_in_tracebacks_mark_cycles_deterministically(self) -> None:
        first = RuntimeError("synthetic-first")
        second = ValueError("synthetic-second")
        first.__cause__ = second
        second.__cause__ = first
        exc_info = (RuntimeError, first, None)

        first_render, first_text = self._render_opt_in_traceback(exc_info)
        second_render, second_text = self._render_opt_in_traceback(exc_info)

        self.assertEqual(first_render, second_render)
        self.assertEqual(first_text, second_text)
        self.assertEqual(first_render.count("[traceback cycle omitted]"), 1)

    def test_opt_in_tracebacks_enforce_total_output_limit(self) -> None:
        exceptions: list[Exception] = []

        def capture_exception(depth: int, exception: Exception) -> None:
            if depth:
                capture_exception(depth - 1, exception)
            raise exception

        capture_exception.__code__ = capture_exception.__code__.replace(
            co_filename="M" * 300 + ".py",
            co_name="render_total_output_probe_" + "N" * 300,
        )
        for index in range(63):
            exception = RuntimeError(f"synthetic-{index}")
            try:
                capture_exception(31, exception)
            except RuntimeError as captured:
                exceptions.append(captured)
        group = ExceptionGroup("synthetic-output-group", exceptions)
        exc_info = (ExceptionGroup, group, None)

        json_traceback, text_line = self._render_opt_in_traceback(exc_info)

        self.assertLessEqual(len(json_traceback), 16_384)
        self.assertIn("[traceback truncated: output limit]", json_traceback)
        self.assertIn(json_traceback, text_line)

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
