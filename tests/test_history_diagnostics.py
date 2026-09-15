from __future__ import annotations

import asyncio
import sqlite3
import unittest

from history_service.diagnostics import (
    MAX_DIAGNOSTIC_SUMMARY_CHARS,
    HistorySourceError,
    classify_collection_failure,
    classify_retention_failure,
)
from history_service.refresh_auth import ManualRefreshAdmission


class CollectionFailureClassificationTests(unittest.TestCase):
    def test_unreachable_source_reports_a_fixed_sentence_without_the_url(self) -> None:
        exc = HistorySourceError.unreachable(
            "GET http://enclosure-ui:8000/api/slots failed: [Errno 111] Connection refused"
        )

        kind, summary = classify_collection_failure(exc)

        self.assertEqual(kind, "source_unreachable")
        self.assertEqual(summary, "Could not reach the main UI service.")
        self.assertNotIn("enclosure-ui", summary)
        self.assertNotIn("Errno", summary)

    def test_timeout_reports_the_configured_timeout_and_no_host(self) -> None:
        exc = HistorySourceError.timeout(
            "GET http://enclosure-ui:8000/api/slots timed out after 45s",
            timeout_seconds=45,
        )

        kind, summary = classify_collection_failure(exc)

        self.assertEqual(kind, "source_timeout")
        self.assertEqual(
            summary,
            "The main UI did not answer within the request timeout. (timeout 45 s)",
        )
        self.assertNotIn("enclosure-ui", summary)

    def test_rejected_request_reports_the_status_code_only(self) -> None:
        exc = HistorySourceError.rejected(
            "GET http://enclosure-ui:8000/api/slots failed with HTTP 500: secret-token=abc",
            status_code=500,
        )

        kind, summary = classify_collection_failure(exc)

        self.assertEqual(kind, "source_rejected")
        self.assertEqual(summary, "The main UI rejected the request. (HTTP 500)")
        self.assertNotIn("secret-token", summary)

    def test_unreadable_and_application_error_replies_have_their_own_sentences(self) -> None:
        self.assertEqual(
            classify_collection_failure(HistorySourceError.bad_payload("bad json at line 1")),
            (
                "source_bad_payload",
                "The main UI returned a response the collector could not read.",
            ),
        )
        self.assertEqual(
            classify_collection_failure(HistorySourceError.error_reply("disk /dev/da3 is missing")),
            ("source_error_reply", "The main UI reported an error for this request."),
        )

    def test_unknown_failures_keep_the_class_name_and_drop_the_message(self) -> None:
        kind, summary = classify_collection_failure(ValueError("/mnt/tank/secret.db is broken"))

        self.assertEqual(kind, "unexpected")
        self.assertEqual(
            summary,
            "Unexpected collector error; see the service logs. (ValueError)",
        )
        self.assertNotIn("secret.db", summary)

    def test_every_summary_is_bounded(self) -> None:
        exc = HistorySourceError.rejected("x" * 5000, status_code=500)

        _, summary = classify_collection_failure(exc)

        self.assertLessEqual(len(summary), MAX_DIAGNOSTIC_SUMMARY_CHARS)


class RetentionFailureClassificationTests(unittest.TestCase):
    def test_batch_size_overflow_names_the_setting_to_lower(self) -> None:
        kind, summary = classify_retention_failure(
            sqlite3.OperationalError("too many SQL variables")
        )

        self.assertEqual(kind, "retention_batch_too_large")
        self.assertEqual(
            summary,
            "Retention batch size is too large; lower HISTORY_RETENTION_BATCH_SIZE. "
            "(OperationalError)",
        )

    def test_read_only_and_full_disks_are_named_in_plain_words(self) -> None:
        self.assertEqual(
            classify_retention_failure(
                sqlite3.OperationalError("attempt to write a readonly database")
            )[0],
            "database_read_only",
        )
        self.assertEqual(
            classify_retention_failure(sqlite3.OperationalError("database or disk is full"))[0],
            "disk_full",
        )
        self.assertEqual(
            classify_retention_failure(sqlite3.OperationalError("disk I/O error"))[0],
            "disk_io_error",
        )
        self.assertEqual(
            classify_retention_failure(sqlite3.OperationalError("database is locked"))[0],
            "database_locked",
        )

    def test_os_errors_are_classified_by_errno_not_by_message(self) -> None:
        permission_error = PermissionError(13, "Permission denied")
        full_error = OSError(28, "No space left on device")

        self.assertEqual(classify_retention_failure(permission_error)[0], "permission_denied")
        self.assertEqual(classify_retention_failure(full_error)[0], "disk_full")

    def test_unknown_retention_failures_never_echo_the_message(self) -> None:
        kind, summary = classify_retention_failure(RuntimeError("/mnt/tank/private.db is broken"))

        self.assertEqual(kind, "unexpected")
        self.assertEqual(
            summary,
            "Unexpected retention error; see the service logs. (RuntimeError)",
        )
        self.assertNotIn("private.db", summary)

    def test_retention_summaries_are_bounded(self) -> None:
        _, summary = classify_retention_failure(RuntimeError("y" * 5000))

        self.assertLessEqual(len(summary), MAX_DIAGNOSTIC_SUMMARY_CHARS)


class ManualRefreshCooldownStateTests(unittest.TestCase):
    def test_cooldown_state_reports_the_deadline_without_consuming_the_cooldown(self) -> None:
        clock = [1000.0]
        admission = ManualRefreshAdmission(cooldown_seconds=900, monotonic=lambda: clock[0])

        async def scenario() -> None:
            self.assertEqual(
                admission.cooldown_state(),
                {"cooldown_seconds": 900, "seconds_remaining": 0, "active": False},
            )
            accepted = await admission.try_acquire("full")
            self.assertTrue(accepted.accepted)
            await admission.release()
            clock[0] += 300.0

            state = admission.cooldown_state()
            self.assertEqual(state["cooldown_seconds"], 900)
            self.assertEqual(state["seconds_remaining"], 600)
            self.assertTrue(state["active"])
            # Reading the state must not move the deadline.
            self.assertEqual(admission.cooldown_state()["seconds_remaining"], 600)

            clock[0] += 600.0
            self.assertEqual(admission.cooldown_state()["seconds_remaining"], 0)
            self.assertFalse(admission.cooldown_state()["active"])

        asyncio.run(scenario())

    def test_cooldown_state_is_inactive_when_the_cooldown_is_disabled(self) -> None:
        admission = ManualRefreshAdmission(cooldown_seconds=0, monotonic=lambda: 5.0)

        self.assertEqual(
            admission.cooldown_state(),
            {"cooldown_seconds": 0, "seconds_remaining": 0, "active": False},
        )


if __name__ == "__main__":
    unittest.main()
