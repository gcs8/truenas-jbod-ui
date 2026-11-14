"""#1170: a failed history collection pass says which stage and error class failed.

The safe log formatters deliberately keep only the exception class, so a
SQLite lock wait, a full disk, an I/O error and a damaged page all looked the
same, and a source failure did not say what it was. The failure record now
carries a fixed, allowlisted set of structured fields. Nothing from the
exception message, SQL text, file paths or host names reaches either format.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.logging_config import JsonFormatter, SafeTextFormatter
from history_service import collector as collector_module
from history_service.collector import HistoryCollector
from history_service.config import HistorySettings
from history_service.diagnostics import HistorySourceError
from history_service.store import HistoryStore

FAILURE_MESSAGE = "History collection pass failed"
SECRET_HOST = "nas-secret-01.example.invalid"
SECRET_PATH = "/srv/secret-pool/history-private.sqlite3"
SECRET_SQL = "INSERT INTO metric_samples (observed_at, system_id) VALUES (?, ?)"
SECRET_PHRASE = "private-diagnostic-phrase"
SECRET_TEXT = f"{SECRET_PHRASE} {SECRET_SQL} at {SECRET_PATH} on {SECRET_HOST}"
FORBIDDEN = (SECRET_HOST, SECRET_PATH, SECRET_SQL, SECRET_PHRASE, "INSERT INTO", "metric_samples")

# Every key the failure record may carry in JSON form. Anything else is a leak.
ALLOWED_JSON_KEYS = {
    "ts",
    "level",
    "logger",
    "message",
    "event",
    "component",
    "exception_class",
    "failure_stage",
    "scope_id",
    "source_error_kind",
    "sqlite_errorcode",
    "sqlite_errorname",
    "sqlite_error_class",
}


def sqlite_error(cls: type[sqlite3.Error], code: int | None, name: str | None) -> sqlite3.Error:
    """A SQLite error shaped like Python 3.11+ raises, with a secret-bearing message."""

    error = cls(SECRET_TEXT)
    if code is not None:
        error.sqlite_errorcode = code
    if name is not None:
        error.sqlite_errorname = name
    return error


class HistoryFailureLogFieldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = HistorySettings(
            sqlite_path=str(self.root / "history.db"),
            backup_dir=str(self.root / "backups"),
            startup_grace_seconds=0,
            poll_interval_seconds=60,
            failure_backoff_initial_seconds=5,
            failure_backoff_max_seconds=12,
        )
        self.store = HistoryStore(self.settings.sqlite_path)
        self.collector = HistoryCollector(self.settings, self.store)
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.inventory_error: BaseException | None = None
        self.trusted = True
        transport = patch.object(HistoryCollector, "_fetch_json_sync", side_effect=self._source)
        transport.start()
        self.addCleanup(transport.stop)

    def _source(self, path, params, method, body, headers, timeout):
        if path == "/api/inventory":
            if self.inventory_error is not None:
                raise self.inventory_error
            system = params.get("system_id", "system-a")
            return {
                "systems": [{"id": "system-a"}],
                "selected_system_id": system,
                "selected_system_platform": "core",
                "sources": {"api": {"enabled": True, "ok": self.trusted}},
                "slots": [
                    {
                        "slot": 0,
                        "present": self.trusted,
                        "serial": "SANITIZED-0" if self.trusted else None,
                        "state": "healthy" if self.trusted else "empty",
                    }
                ],
            }
        if path == "/api/storage-views":
            return {"views": []}
        if path.endswith("smart-batch"):
            return {
                "summaries": [
                    {
                        "slot": 0,
                        "summary": {
                            "available": True,
                            "smart_health_status": "PASSED",
                            "temperature_c": 30,
                            "power_on_hours": 100,
                        },
                    }
                ]
            }
        raise AssertionError(f"Unexpected synthetic request: {path}")

    async def _failed_pass_record(self) -> logging.LogRecord:
        async def stop_after_first_pass():
            self.collector._stopping.set()
            raise asyncio.TimeoutError

        with (
            patch("history_service.collector.utcnow", side_effect=lambda: self.now),
            patch.object(self.collector._stopping, "wait", side_effect=stop_after_first_pass),
            self.assertLogs("history_service.collector", level="ERROR") as captured,
        ):
            await asyncio.wait_for(self.collector._run_loop(), timeout=30)
        failures = [record for record in captured.records if record.getMessage() == FAILURE_MESSAGE]
        self.assertEqual(len(failures), 1, [record.getMessage() for record in captured.records])
        return failures[0]

    def _rendered(self, record: logging.LogRecord) -> tuple[dict[str, Any], str]:
        payload = json.loads(JsonFormatter(service_name="enclosure-history").format(record))
        text = SafeTextFormatter(service_name="enclosure-history").format(record)
        payload.pop("service", None)
        for rendered in (json.dumps(payload), text):
            for secret in FORBIDDEN:
                self.assertNotIn(secret, rendered)
            self.assertNotIn("Traceback", rendered)
        self.assertLessEqual(set(payload), ALLOWED_JSON_KEYS, payload)
        self.assertEqual(payload["message"], FAILURE_MESSAGE)
        self.assertEqual(payload["event"], "history_collection_failed")
        for key, value in payload.items():
            if key in {"ts", "level", "logger", "message"}:
                continue
            self.assertIn(f"{key}={value}", text)
        return payload, text

    async def test_slot_state_write_sqlite_lock_names_stage_scope_and_sqlite_code(self) -> None:
        error = sqlite_error(sqlite3.OperationalError, 5, "SQLITE_BUSY")
        with patch.object(HistoryCollector, "_record_slot_changes", side_effect=error):
            payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["exception_class"], "OperationalError")
        self.assertEqual(payload["failure_stage"], "slot_state_write")
        self.assertEqual(payload["scope_id"], "system-a")
        self.assertEqual(payload["sqlite_errorcode"], 5)
        self.assertEqual(payload["sqlite_errorname"], "SQLITE_BUSY")
        self.assertEqual(payload["sqlite_error_class"], "busy_locked")
        self.assertNotIn("source_error_kind", payload)

    async def test_metric_write_real_sqlite_lock_is_classified_from_sqlite_itself(self) -> None:
        locked_path = self.root / "locked.sqlite3"
        with closing(sqlite3.connect(locked_path)) as holder:
            holder.execute("CREATE TABLE sample (value INTEGER)")
            holder.commit()
            holder.execute("BEGIN EXCLUSIVE")

            def write_against_lock(samples):
                with closing(sqlite3.connect(locked_path, timeout=0)) as writer:
                    writer.execute("INSERT INTO sample (value) VALUES (1)")

            with patch.object(self.store, "insert_metric_samples", side_effect=write_against_lock):
                payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["failure_stage"], "metric_write")
        self.assertEqual(payload["scope_id"], "system-a")
        self.assertEqual(payload["sqlite_error_class"], "busy_locked")
        self.assertEqual(payload["sqlite_errorcode"] & 0xFF, 5)
        self.assertTrue(payload["sqlite_errorname"].startswith("SQLITE_BUSY"))

    async def test_backup_corruption_names_backup_stage_and_corrupt_class(self) -> None:
        # A non-startup pass collects slow metrics, so the backup stage runs.
        self.collector._startup_collection_attempted = True
        error = sqlite_error(sqlite3.DatabaseError, 11, "SQLITE_CORRUPT")
        error.args = (f"database disk image is malformed: {SECRET_TEXT}",)
        with patch.object(self.store, "create_backup", side_effect=error):
            payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["exception_class"], "DatabaseError")
        self.assertEqual(payload["failure_stage"], "backup")
        self.assertEqual(payload["sqlite_errorcode"], 11)
        self.assertEqual(payload["sqlite_error_class"], "corrupt")
        self.assertNotIn("scope_id", payload)

    async def test_post_pass_status_read_failure_is_not_blamed_on_collection(self) -> None:
        error = sqlite_error(sqlite3.OperationalError, 13, "SQLITE_FULL")
        with patch.object(self.store, "estimated_counts", side_effect=error):
            payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["failure_stage"], "post_pass_status")
        self.assertEqual(payload["sqlite_error_class"], "full")
        self.assertNotIn("scope_id", payload)

    async def test_wrapped_sqlite_error_without_codes_gets_a_coarse_class_only(self) -> None:
        cause = sqlite3.OperationalError(f"disk I/O error {SECRET_TEXT}")

        def wrapped(*args, **kwargs):
            raise RuntimeError(SECRET_TEXT) from cause

        with patch.object(HistoryCollector, "_record_slot_changes", side_effect=wrapped):
            payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["exception_class"], "RuntimeError")
        self.assertEqual(payload["sqlite_error_class"], "io")
        self.assertNotIn("sqlite_errorcode", payload)
        self.assertNotIn("sqlite_errorname", payload)

    async def test_root_inventory_timeout_names_inventory_stage_and_source_kind(self) -> None:
        self.inventory_error = HistorySourceError.timeout(
            f"GET https://{SECRET_HOST}/api/inventory {SECRET_PHRASE}",
            timeout_seconds=30,
        )
        payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["exception_class"], "HistorySourceError")
        self.assertEqual(payload["failure_stage"], "inventory_read")
        self.assertEqual(payload["source_error_kind"], "source_timeout")
        self.assertNotIn("sqlite_error_class", payload)

    async def test_no_trusted_scope_names_enumeration_stage_and_scope(self) -> None:
        self.trusted = False
        payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["failure_stage"], "scope_enumeration")
        self.assertEqual(payload["source_error_kind"], "source_error_reply")

    async def test_unsafe_scope_identifier_is_omitted_rather_than_logged(self) -> None:
        original_source = self._source

        def unsafe_ids(path, params, method, body, headers, timeout):
            payload: dict[str, Any] = original_source(path, params, method, body, headers, timeout)
            if path == "/api/inventory":
                payload["selected_system_id"] = f"{SECRET_PHRASE} {SECRET_PATH}"
                payload["systems"] = [{"id": payload["selected_system_id"]}]
            return payload

        error = sqlite_error(sqlite3.OperationalError, 6, "SQLITE_LOCKED")
        with (
            patch.object(HistoryCollector, "_fetch_json_sync", side_effect=unsafe_ids),
            patch.object(HistoryCollector, "_record_slot_changes", side_effect=error),
        ):
            payload, _ = self._rendered(await self._failed_pass_record())
        self.assertEqual(payload["failure_stage"], "slot_state_write")
        self.assertEqual(payload["sqlite_error_class"], "busy_locked")
        self.assertNotIn("scope_id", payload)


class SqliteFailureClassificationTests(unittest.TestCase):
    def test_primary_and_extended_codes_map_to_fixed_classes(self) -> None:
        cases = {
            (5, "SQLITE_BUSY"): "busy_locked",
            (6, "SQLITE_LOCKED"): "busy_locked",
            (517, "SQLITE_BUSY_SNAPSHOT"): "busy_locked",
            (13, "SQLITE_FULL"): "full",
            (10, "SQLITE_IOERR"): "io",
            (522, "SQLITE_IOERR_SHORT_READ"): "io",
            (11, "SQLITE_CORRUPT"): "corrupt",
            (26, "SQLITE_NOTADB"): "corrupt",
            (1, "SQLITE_ERROR"): "other",
            (8, "SQLITE_READONLY"): "other",
        }
        for (code, name), expected in cases.items():
            with self.subTest(name=name):
                fields = collector_module.collection_failure_log_fields(
                    sqlite_error(sqlite3.OperationalError, code, name),
                    stage="metric_write",
                    scope_id=None,
                )
                self.assertEqual(fields["sqlite_errorcode"], code)
                self.assertEqual(fields["sqlite_errorname"], name)
                self.assertEqual(fields["sqlite_error_class"], expected)

    def test_unknown_stage_and_unsafe_error_name_are_dropped(self) -> None:
        fields = collector_module.collection_failure_log_fields(
            sqlite_error(sqlite3.OperationalError, 5, f"SQLITE_BUSY {SECRET_PATH}"),
            stage=f"stage {SECRET_PATH}",
            scope_id="system-a",
        )
        self.assertEqual(fields["failure_stage"], "unknown")
        self.assertNotIn("sqlite_errorname", fields)
        self.assertEqual(fields["sqlite_error_class"], "busy_locked")
        for value in fields.values():
            self.assertNotIn(SECRET_PATH, str(value))

    def test_non_sqlite_non_source_errors_carry_only_stage_and_scope(self) -> None:
        fields = collector_module.collection_failure_log_fields(
            ValueError(SECRET_TEXT), stage="smart_read", scope_id="system-a"
        )
        self.assertEqual(
            fields,
            {
                "event": "history_collection_failed",
                "component": "enclosure-history",
                "failure_stage": "smart_read",
                "scope_id": "system-a",
            },
        )


if __name__ == "__main__":
    unittest.main()
